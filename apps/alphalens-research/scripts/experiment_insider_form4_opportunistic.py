"""insider_form4_opportunistic_2026_05_05 — long-only top-decile Form-4 scorer.

Pre-reg context:
- Class: insider_form4_opportunistic_2026_05_05 (NEW, fresh single-class)
- Mechanism: Cohen-Malloy-Pomorski 2012 (JFE p. 1786) opportunistic-insider
  net-buy magnitude residualized cross-sectionally vs equity controls.
- Primary universe: R2000 PIT; secondary diagnostic R3000.
- Per-reg gates: PASS = mean αt ≥ 2.86 AND every-phase αt ≥ 1.5 AND mean
  excess_net_ann ≥ 0 AND dispersion ≤ 70pp on R2000.

CLI flags mirror experiment_distress_credit_v1.py for parity with the
audit_multi_phase.py regex parser.

Requires the SEC EDGAR Form-4 backfill at ``~/.alphalens/form4_parquet/``.
If the backfill is missing or empty, exits non-zero with a clear message
(no silent zero-return runs).
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

from alphalens_pipeline.data.alt_data.pit_universe_loader import (  # noqa: E402
    load_universe_union,
)
from alphalens_pipeline.data.alt_data.ticker_cik_map import TickerCikMap  # noqa: E402
from alphalens_pipeline.data.alt_data.yfinance_cache import load_cached_histories  # noqa: E402
from alphalens_pipeline.data.factors import load_carhart_daily  # noqa: E402
from alphalens_pipeline.data.store.form4_pit import (  # noqa: E402
    PARTITION_KEY,
    Form4PITStore,
)
from alphalens_pipeline.data.store.history import HistoryStore  # noqa: E402
from alphalens_pipeline.scorers.cohen_malloy_classifier import (  # noqa: E402
    CohenMalloyLabel,
    classify_from_transaction_dates,
)
from alphalens_pipeline.scorers.opportunistic_form4 import (  # noqa: E402
    aggregate_opportunistic_signal,
    score_opportunistic_form4,
)
from alphalens_research.attribution.cost_model import RealisticCostModel  # noqa: E402
from alphalens_research.attribution.factor_analysis import run_regression  # noqa: E402
from alphalens_research.backtest.daily_continuous_returns import (  # noqa: E402
    daily_continuous_returns,
)
from alphalens_research.backtest.engine import BacktestEngine  # noqa: E402
from alphalens_research.backtest.metrics import (  # noqa: E402
    per_rebalance_turnover,
    sharpe,
    turnover_pct,
)
from alphalens_research.screeners.distress_credit.features import (  # noqa: E402
    make_production_stores,
)
from alphalens_research.screeners.options_implied.features import (  # noqa: E402
    _compute_equity_controls,
)

logger = logging.getLogger(__name__)

_PRICES_DIR = Path.home() / ".alphalens" / "prices"
_FORM4_PARQUET_DEFAULT = Path.home() / ".alphalens" / "form4_parquet"
_CARHART_FACTORS = ["Mkt-RF", "SMB", "HML", "Mom"]
_HAC_MAXLAGS_LOCK = 126  # per pre-reg lock; matches 6m signal window


class ClassifierCache:
    """Caches Cohen-Malloy labels per (person_cik, year_y)."""

    def __init__(self, store: Form4PITStore):
        self._store = store
        self._cache: dict[tuple[str, int], CohenMalloyLabel] = {}

    def get(self, person_cik: str, classification_year: int) -> CohenMalloyLabel:
        key = (person_cik, classification_year)
        if key not in self._cache:
            history = self._store.records_for_person(person_cik, classification_year)
            dates = history["transaction_date"].tolist() if not history.empty else []
            self._cache[key] = classify_from_transaction_dates(
                dates, classification_year=classification_year
            )
        return self._cache[key]


class _StaticTickerCikResolver:
    """Wrap the offline ticker_cik_map.yaml as a CIK resolver for Form4PITStore."""

    def __init__(self, mapping: dict[str, str]):
        self._mapping = {k.upper(): v for k, v in mapping.items()}

    def lookup(self, ticker: str) -> str | None:
        return self._mapping.get(ticker.upper())


def _check_backfill_exists(parquet_root: Path) -> None:
    if not parquet_root.is_dir():
        sys.stderr.write(
            f"ERROR: Form-4 parquet backfill missing at {parquet_root}.\n"
            "Run the SEC EDGAR backfill on runpod first; see "
            "docs/research/insider_form4_opportunistic_runpod_handoff.md\n"
        )
        sys.exit(2)
    if not list(parquet_root.glob(f"{PARTITION_KEY}=*")):
        sys.stderr.write(f"ERROR: Form-4 parquet root {parquet_root} has no partitions.\n")
        sys.exit(2)


def benchmark_returns(
    history_store: HistoryStore, benchmark: str, start: date, end: date
) -> pd.Series:
    df = history_store.full(benchmark)
    df = df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
    return df["close"].pct_change().dropna()


def _close_at(history: pd.DataFrame, asof: pd.Timestamp) -> float | None:
    sliced = history[history.index <= asof]
    if sliced.empty:
        return None
    return float(sliced["close"].iloc[-1])


class _OpportunisticForm4Scorer:
    """BacktestEngine adapter — for each rebalance asof, build features and score."""

    def __init__(
        self,
        *,
        form4_store: Form4PITStore,
        classifier_cache: ClassifierCache,
        mcap_lookup: dict[str, float] | None = None,
        shares_store=None,
    ):
        self._store = form4_store
        self._cache = classifier_cache
        self._shares = shares_store
        self._mcap_lookup = mcap_lookup or {}

    def __call__(self, histories, config=None) -> pd.DataFrame:
        cfg = dict(config or {})
        asof = cfg.get("asof")
        if asof is None:
            common = None
            for df in histories.values():
                if df is None or len(df) == 0:
                    continue
                if common is None or df.index[-1] > common:
                    common = df.index[-1]
            asof = common
        if asof is None:
            return pd.DataFrame(columns=["ticker", "score"])

        asof_date = asof.date() if hasattr(asof, "date") else asof
        asof_ts = pd.Timestamp(asof_date)

        rows: list[dict] = []
        for ticker, history in histories.items():
            if history is None or history.empty:
                continue
            sliced = history[history.index <= asof_ts]
            if sliced.empty:
                continue

            controls = _compute_equity_controls(sliced)
            if controls is None:
                continue

            close = _close_at(sliced, asof_ts)
            if close is None or close <= 0:
                continue

            # Market cap proxy. If shares_store provided, use it; else fall
            # back to a precomputed mcap_lookup. If neither, use close × 1
            # (degenerate; orchestrator should always supply shares).
            shares = None
            if self._shares is not None:
                shares = self._shares.get(ticker, asof_ts)
            mcap = (close * shares) if shares else self._mcap_lookup.get(ticker)
            if not mcap or mcap <= 0:
                continue

            records = self._store.records_as_of(ticker, asof=asof_date, lookback_days=180)
            net_oppor_usd = aggregate_opportunistic_signal(
                records, asof=asof_date, classifier_cache=self._cache
            )
            signal_raw = float(net_oppor_usd) / float(mcap)

            rows.append(
                {
                    "asof": asof_date,
                    "ticker": ticker,
                    "signal_raw": signal_raw,
                    **controls,
                }
            )

        if not rows:
            return pd.DataFrame(columns=["ticker", "score"])

        features = pd.DataFrame(rows)
        features["score"] = score_opportunistic_form4(features)
        return features[["ticker", "score"]].dropna(subset=["score"])


def assess(
    report,
    factors,
    rebalance_stride,
    cost_bps,
    bench_rets_daily,
    *,
    history_store,
    benchmark,
    end_date,
) -> dict:
    """Daily-cadence Carhart attribution per pre-reg lock v2 (2026-05-08).

    Pre-reg ledger ``insider_form4_opportunistic_2026_05_08_v2`` mandates
    a daily continuous-holding return series (~1500 obs over 6yr OOS) so
    that ``hac_maxlags=126`` (trading days) sits in the correct sample
    unit for the Newey-West kernel. v1 fed rebalance-cadence (~72 obs)
    here, which silently inflated t-stats ~3x — see v1 ledger ``outcome``.
    """
    rets_daily = daily_continuous_returns(
        report.rebalance_results,
        history_store,
        calendar_ticker=benchmark,
        end_date=end_date,
    )
    if rets_daily.empty:
        return {"n": 0}

    avg_turnover = turnover_pct(r.top_n_tickers for r in report.rebalance_results)
    rebalances_per_year = 252 / max(1, rebalance_stride)
    cost_model = RealisticCostModel(adverse_selection_bps=5.0)
    drag_per_rebal_bps = cost_model.primary_period_drag_bps(cost_bps, avg_turnover)
    drag_ann = drag_per_rebal_bps * rebalances_per_year / 10_000.0
    drag_per_day = drag_ann / 252.0
    rets_net_daily = rets_daily - drag_per_day

    sharpe_gross = sharpe(rets_daily.tolist(), periods_per_year=252)
    sharpe_net = sharpe(rets_net_daily.tolist(), periods_per_year=252)

    # HAC override per pre-reg lock — 6m signal window dictates 126-day max lag
    # at DAILY cadence (~1500 obs).
    res4 = run_regression(
        rets_daily,
        factors[[*_CARHART_FACTORS, "RF"]],
        _CARHART_FACTORS,
        hac_maxlags=_HAC_MAXLAGS_LOCK,
        periods_per_year=252,
    )

    bench_aligned = bench_rets_daily.reindex(rets_daily.index).dropna()
    excess_per_day = (rets_daily.reindex(bench_aligned.index) - bench_aligned).mean()
    excess_ann = float(excess_per_day * 252) if not np.isnan(excess_per_day) else float("nan")

    mean_top_n = float(
        sum(len(r.top_n_tickers) for r in report.rebalance_results)
        / max(1, len(report.rebalance_results))
    )
    turnover_series = per_rebalance_turnover(
        (r.top_n_tickers for r in report.rebalance_results),
        dates=[r.date for r in report.rebalance_results],
    )

    return {
        "n": len(rets_daily),
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
        "rets_daily": rets_daily,
        "turnover_series": turnover_series,
    }


def derive_turnover_path(returns_path: Path) -> Path:
    """Derive co-located turnover parquet path from a returns parquet path.

    Slippage stress diagnostic 2026-05-12: per-phase turnover persistence
    rides on the existing ``--dump-returns`` plumbing. If the returns path
    stem contains ``returns``, replace the LAST occurrence with ``turnover``;
    otherwise append ``_turnover`` to the stem. Replacing only the last
    occurrence is intentional — a path like ``returns_returns.parquet``
    would otherwise become ``turnover_turnover.parquet`` (writing to an
    unintended location).
    """
    stem = returns_path.stem
    if "returns" in stem:
        new_stem = "turnover".join(stem.rsplit("returns", 1))
    else:
        new_stem = f"{stem}_turnover"
    return returns_path.with_name(f"{new_stem}{returns_path.suffix}")


def dump_turnover_parquet(turnover_df: pd.DataFrame, returns_path: Path) -> Path:
    """Write per-rebalance turnover parquet alongside returns parquet.

    Returns the path written. Creates parents if needed.
    """
    out_path = derive_turnover_path(returns_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    turnover_df.to_parquet(out_path)
    return out_path


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--top-n", type=int, default=200)
    ap.add_argument("--holding", type=int, default=21)
    ap.add_argument("--rebalance-stride", type=int, default=21)
    ap.add_argument("--benchmark", default="IWM")
    ap.add_argument("--cost-half-spreads", nargs="+", type=float, default=[5.0])
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("docs/research/insider_form4_opportunistic_audit_run.md"),
    )
    ap.add_argument("--is-start", type=date.fromisoformat, default=date(2018, 1, 1))
    ap.add_argument("--is-end", type=date.fromisoformat, default=date(2023, 12, 31))
    ap.add_argument("--phase-offset", type=int, default=0)
    ap.add_argument(
        "--parquet-root",
        type=Path,
        default=_FORM4_PARQUET_DEFAULT,
    )
    ap.add_argument(
        "--universe-mode",
        choices=["R2000", "R3000"],
        default="R2000",
        help="R2000 PIT primary (per pre-reg) or R3000 diagnostic.",
    )
    ap.add_argument(
        "--dump-returns",
        type=Path,
        default=None,
        help="Optional parquet path; when set, writes report.portfolio_returns "
        "for downstream block-bootstrap (Phase B G5 Romano-Wolf). No-op if unset.",
    )
    return ap


def main() -> int:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    _check_backfill_exists(args.parquet_root)

    if args.universe_mode == "R3000":
        sys.stderr.write(
            "ERROR: R3000 mode requires separately-built PIT yaml snapshots "
            "(wider mcap band $50M-$1T). Current ~/.alphalens/pit_universe/ "
            "yamls are R2000-band only. Build R3000 yamls via "
            "scripts/build_pit_universe.py with --cap-min/--cap-max overrides "
            "before running R3000 diagnostic.\n"
        )
        return 4

    logger.info(
        "experiment insider_form4_opportunistic | universe=%s | %s..%s | phase_offset=%d",
        args.universe_mode,
        args.is_start,
        args.is_end,
        args.phase_offset,
    )

    # 1. PIT universe union over the study window (R2000 from yaml snapshots).
    #    The yamls were pre-built by scripts/build_pit_universe.py to enforce
    #    the $300M-$3B mcap band per Cohen-Malloy size-quintile localization.
    universe = load_universe_union(args.is_start, args.is_end)
    if not universe:
        sys.stderr.write(
            "ERROR: load_universe_union returned empty list for "
            f"{args.is_start}..{args.is_end}. PIT yaml snapshots missing at "
            "~/.alphalens/pit_universe/.\n"
        )
        return 5
    logger.info("PIT universe union: %d unique tickers across window", len(universe))

    # 2. OHLCV histories for full union + benchmark (cached from yfinance).
    histories = load_cached_histories([*universe, args.benchmark], _PRICES_DIR)
    if args.benchmark not in histories or histories[args.benchmark].empty:
        sys.stderr.write(f"ERROR: benchmark {args.benchmark} OHLCV missing from {_PRICES_DIR}.\n")
        return 6
    history_store = HistoryStore(histories)

    # 3. Production stores: shares (for mcap) + ticker_cik_map (for Form-4 lookup).
    _liab_store, share_store = make_production_stores()
    tcm_path = (
        REPO_ROOT / "alphalens_research" / "data" / "alt_data" / "data" / "ticker_cik_map.yaml"
    )
    cik_resolver = TickerCikMap.load(tcm_path)

    # 4. Form-4 PIT store + Cohen-Malloy classifier cache.
    form4_store = Form4PITStore(
        parquet_root=args.parquet_root,
        ticker_cik_resolver=cik_resolver,
        delisting_events=None,  # TODO: wire DelistingEvent loader once available
    )
    classifier_cache = ClassifierCache(form4_store)

    # 5. Scorer adapter — per-rebalance feature build + residualization.
    scorer = _OpportunisticForm4Scorer(
        form4_store=form4_store,
        classifier_cache=classifier_cache,
        shares_store=share_store,
    )

    # 6. Backtest engine — monthly stride 21d, top-decile equal-weight long-only.
    engine = BacktestEngine(
        history_store=history_store,
        scorer=scorer,
        scorer_config={},
        holding_period=args.holding,
        top_n=args.top_n,
        benchmark=args.benchmark,
        screener_tickers=universe,
        weighting="equal",
        rebalance_stride=args.rebalance_stride,
        phase_offset=args.phase_offset,
    )
    logger.info(
        "Engine: stride=%d holding=%d top_n=%d phase_offset=%d benchmark=%s",
        args.rebalance_stride,
        args.holding,
        args.top_n,
        args.phase_offset,
        args.benchmark,
    )
    report = engine.run(args.is_start, args.is_end)

    # 7. Carhart factors + benchmark + assess.
    carhart = load_carhart_daily(start=args.is_start, end=args.is_end)
    bench_rets = benchmark_returns(history_store, args.benchmark, args.is_start, args.is_end)

    period_label = f"OOS {args.is_start.year}-{args.is_end.year}"
    sections: list[str] = [
        f"# insider_form4_opportunistic_2026_05_05 — {period_label}",
        "",
        "Long-only top-decile equal-weighted Form-4 opportunistic-insider scorer",
        "(Cohen-Malloy 2012, residualized vs equity controls).",
        f"Universe: {args.universe_mode} PIT, {len(universe)} ticker union over window.",
        f"Rebalance stride: {args.rebalance_stride}d, holding: {args.holding}d.",
        f"Phase offset: {args.phase_offset}",
        "",
    ]

    all_rows: list[dict] = []
    for cost_bps in args.cost_half_spreads:
        stats = assess(
            report,
            carhart,
            args.rebalance_stride,
            cost_bps,
            bench_rets,
            history_store=history_store,
            benchmark=args.benchmark,
            end_date=args.is_end,
        )
        stats["period"] = period_label
        stats["cost_bps"] = cost_bps
        all_rows.append(stats)
        if stats.get("n", 0) > 0:
            # Canonical line for audit_multi_phase regex parser
            logger.info(
                "%s | cost=%.0fbps | n=%d topN=%.1f turn=%.1f%% | "
                "Sh gross=%.2f net=%.2f | excess gross=%.1f%% net=%.1f%% | "
                "α 4F=%.1f%% t=%.2f",
                period_label,
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
            "{sg:.2f} | {sn:.2f} | {eg:+.1f}% | {en:+.1f}% | {a4:+.1f}% | "
            "{t4:+.2f} | {bsmb:+.2f} | {bhml:+.2f} | {bmom:+.2f} |".format(
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

    if args.dump_returns is not None:
        # v2 ledger lock: dump DAILY continuous-holding returns (not the
        # rebalance-cadence series) so the orchestrator's block-bootstrap
        # operates in the correct unit (block_size=126 trading days).
        rets_daily = next(
            (r["rets_daily"] for r in all_rows if "rets_daily" in r),
            None,
        )
        if rets_daily is None or rets_daily.empty:
            logger.warning("No daily returns to dump (assess returned n=0)")
        else:
            args.dump_returns.parent.mkdir(parents=True, exist_ok=True)
            rets_daily.rename("portfolio_daily").to_frame().to_parquet(args.dump_returns)
            logger.info(
                "Dumped %d daily portfolio return obs to %s",
                len(rets_daily),
                args.dump_returns,
            )

            # Slippage stress diagnostic 2026-05-12: co-locate per-rebalance
            # turnover parquet so the diagnostic can apply regime-conditional
            # cost shocks without smearing Q5-panic turnover clusters via
            # forward-fill from a scalar.
            turnover_df = next(
                (r["turnover_series"] for r in all_rows if "turnover_series" in r),
                None,
            )
            if turnover_df is None or turnover_df.empty:
                logger.warning("No per-rebalance turnover series to dump")
            else:
                turnover_path = dump_turnover_parquet(turnover_df, args.dump_returns)
                logger.info(
                    "Dumped %d per-rebalance turnover rows to %s",
                    len(turnover_df),
                    turnover_path,
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())

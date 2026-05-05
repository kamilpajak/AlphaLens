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

from alphalens.attribution.cost_model import RealisticCostModel  # noqa: E402
from alphalens.attribution.factor_analysis import run_regression  # noqa: E402
from alphalens.backtest.metrics import sharpe, turnover_pct  # noqa: E402
from alphalens.data.store.form4_pit import (  # noqa: E402
    PARTITION_KEY,
    Form4PITStore,
)
from alphalens.data.store.history import HistoryStore  # noqa: E402
from alphalens.screeners.insider_activity.cohen_malloy_classifier import (  # noqa: E402
    CohenMalloyLabel,
    classify_from_transaction_dates,
)
from alphalens.screeners.insider_activity.opportunistic_form4 import (  # noqa: E402
    aggregate_opportunistic_signal,
    score_opportunistic_form4,
)
from alphalens.screeners.options_implied.features import (  # noqa: E402
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

    # HAC override per pre-reg lock — 6m signal window dictates 126-day max lag
    res4 = run_regression(
        rets,
        factors[[*_CARHART_FACTORS, "RF"]],
        _CARHART_FACTORS,
        hac_maxlags=_HAC_MAXLAGS_LOCK,
        periods_per_year=int(rebalances_per_year),
    )

    bench_aligned = bench_rets.reindex(rets.index).dropna()
    excess_per_rebal = (rets.reindex(bench_aligned.index) - bench_aligned).mean()
    excess_ann = (
        float(excess_per_rebal * rebalances_per_year)
        if not np.isnan(excess_per_rebal)
        else float("nan")
    )

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
    return ap


def main() -> int:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    _check_backfill_exists(args.parquet_root)

    logger.info(
        "experiment insider_form4_opportunistic | universe=%s | %s..%s | phase_offset=%d",
        args.universe_mode,
        args.is_start,
        args.is_end,
        args.phase_offset,
    )

    # NOTE: full integration with R2000 PIT universe loader, ticker→CIK
    # resolver, and shares_store happens after backfill completes. This
    # scaffold exits non-zero so audit_multi_phase orchestrator does not
    # silently parse zero rows on incomplete setup.
    sys.stderr.write(
        "ERROR: experiment scaffold present but R2000 PIT universe "
        "integration pending. Wire up after SEC EDGAR backfill completes; "
        "see docs/research/insider_form4_opportunistic_runpod_handoff.md\n"
    )
    return 3


if __name__ == "__main__":
    sys.exit(main())

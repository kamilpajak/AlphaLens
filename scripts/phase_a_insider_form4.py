"""Phase A check for insider_form4_opportunistic_2026_05_05.

Three pre-committed gates per
``docs/research/preregistration/params_insider_form4_opportunistic_2026_05_05.json``
section ``success_criteria.auto_pivot_phase_a_triggers``:

  * A0 Density   — median opportunistic-insider transaction count per R2000
    ticker per quarter on TRAIN 2009-2017 must be >= 2. Below threshold →
    DENSITY-FAIL → ABANDON.
  * A1 Breadth   — >= 30% of asof-quarters on TRAIN 2009-2017 must have
    >= 50 R2000 tickers with score_t != NaN. Below threshold → BREADTH-FAIL
    → ABANDON.
  * A2 Direction — Spearman rho(score_t, forward_21d_excess_return) on TRAIN
    must be > -0.05. Below threshold → SIGN-FLIP → ABANDON.

Emits structured JSON to ``--out`` so the orchestrator can ABANDON cleanly
without burning multi-phase compute.

Requires the SEC EDGAR Form-4 backfill at ``~/.alphalens/form4_parquet/``.
If the backfill directory is missing or empty, exits non-zero with a clear
message instead of silently passing.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from alphalens.data.alt_data.pit_universe_loader import (  # noqa: E402
    load_pit_universe_for_asof,
    load_universe_union,
)
from alphalens.data.alt_data.ticker_cik_map import TickerCikMap  # noqa: E402
from alphalens.data.alt_data.yfinance_cache import load_cached_histories  # noqa: E402
from alphalens.data.store.form4_pit import (  # noqa: E402
    PARTITION_KEY,
    Form4PITStore,
)
from alphalens.screeners.distress_credit.features import (  # noqa: E402
    make_production_stores,
)
from alphalens.screeners.insider_activity.cohen_malloy_classifier import (  # noqa: E402
    CohenMalloyLabel,
    classify_from_transaction_dates,
)

_PRICES_DIR = Path.home() / ".alphalens" / "prices"

logger = logging.getLogger(__name__)


class ClassifierCache:
    """Caches Cohen-Malloy labels per (person_cik, year_y) to avoid
    re-running the 3-year history walk inside aggregation hot loops."""

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


def _check_backfill_exists(parquet_root: Path) -> None:
    if not parquet_root.is_dir():
        sys.stderr.write(
            f"ERROR: Form-4 parquet backfill missing at {parquet_root}.\n"
            "Run the SEC EDGAR backfill on runpod first; see "
            "docs/research/insider_form4_opportunistic_runpod_handoff.md\n"
        )
        sys.exit(2)
    partition_dirs = list(parquet_root.glob(f"{PARTITION_KEY}=*"))
    if not partition_dirs:
        sys.stderr.write(
            f"ERROR: Form-4 parquet root {parquet_root} has no partitions.\n"
            "Backfill is incomplete.\n"
        )
        sys.exit(2)


def _build_quarter_calendar(start: date, end: date) -> list[date]:
    """First-of-quarter business days between ``start`` and ``end`` inclusive."""
    quarters = pd.date_range(start=start, end=end, freq="QS")
    return [d.date() for d in quarters]


def _opportunistic_count_per_ticker(
    *,
    store: Form4PITStore,
    cache: ClassifierCache,
    universe: list[str],
    asof: date,
    lookback_days: int = 90,
) -> dict[str, int]:
    """Count distinct opportunistic-insider transactions per ticker over the
    quarter ending at ``asof``. Used by A0 density check.
    """
    counts: dict[str, int] = {}
    classification_year = asof.year
    for ticker in universe:
        records = store.records_as_of(ticker, asof=asof, lookback_days=lookback_days)
        if records.empty:
            counts[ticker] = 0
            continue
        n = 0
        for row in records.itertuples(index=False):
            if row.transaction_code not in {"P", "S"}:
                continue
            if not (row.is_officer or row.is_director):
                continue
            label = cache.get(row.reporting_owner_cik, classification_year)
            if label is CohenMalloyLabel.OPPORTUNISTIC:
                n += 1
        counts[ticker] = n
    return counts


def _check_density(counts_per_quarter: list[dict[str, int]]) -> dict:
    """A0 density gate: median opportunistic txn count per ticker per quarter >= 2."""
    medians = []
    for counts in counts_per_quarter:
        vals = [n for n in counts.values() if n > 0]  # ignore zero-activity tickers
        if not vals:
            continue
        medians.append(float(np.median(vals)))
    overall_median = float(np.median(medians)) if medians else 0.0
    return {
        "gate_id": "A0_density",
        "median_opportunistic_count_per_active_ticker_per_quarter": overall_median,
        "threshold": 2.0,
        "passed": overall_median >= 2.0,
        "n_quarters_evaluated": len(medians),
    }


def _check_breadth(scored_counts_per_quarter: list[int]) -> dict:
    """A1 breadth gate: >= 30% of quarters have >= 50 scored tickers."""
    if not scored_counts_per_quarter:
        return {"gate_id": "A1_breadth", "passed": False, "frac_above_threshold": 0.0}
    above = sum(1 for c in scored_counts_per_quarter if c >= 50)
    frac = above / len(scored_counts_per_quarter)
    return {
        "gate_id": "A1_breadth",
        "frac_quarters_with_at_least_50_scored_tickers": frac,
        "threshold": 0.30,
        "passed": frac >= 0.30,
        "n_quarters_evaluated": len(scored_counts_per_quarter),
    }


def _check_direction(corr: float) -> dict:
    """A2 direction gate: Spearman rho(score, fwd_21d_excess) > -0.05."""
    return {
        "gate_id": "A2_direction",
        "spearman_rho_score_vs_fwd_21d_excess": corr,
        "threshold": -0.05,
        "passed": corr > -0.05,
    }


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--train-start", type=date.fromisoformat, default=date(2009, 1, 1))
    ap.add_argument("--train-end", type=date.fromisoformat, default=date(2017, 12, 31))
    ap.add_argument(
        "--parquet-root",
        type=Path,
        default=Path.home() / ".alphalens" / "form4_parquet",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("docs/research/insider_form4_opportunistic_phase_a.json"),
    )
    ap.add_argument(
        "--universe-mode",
        choices=["R2000", "R3000"],
        default="R2000",
        help="R2000 PIT primary (per pre-reg) or R3000 diagnostic.",
    )
    return ap


def _forward_excess_return(
    history: pd.DataFrame,
    bench_history: pd.DataFrame,
    asof: pd.Timestamp,
    horizon_days: int = 21,
) -> float | None:
    """21-trading-day forward excess return: ticker minus benchmark."""
    end = asof + pd.Timedelta(days=int(horizon_days * 1.5))  # calendar slack
    tkr_window = history[(history.index > asof) & (history.index <= end)]
    bch_window = bench_history[(bench_history.index > asof) & (bench_history.index <= end)]
    if len(tkr_window) < horizon_days or len(bch_window) < horizon_days:
        return None
    px_t0 = float(history[history.index <= asof].iloc[-1]["close"])
    bx_t0 = float(bench_history[bench_history.index <= asof].iloc[-1]["close"])
    if px_t0 <= 0 or bx_t0 <= 0:
        return None
    px_tn = float(tkr_window.iloc[horizon_days - 1]["close"])
    bx_tn = float(bch_window.iloc[horizon_days - 1]["close"])
    return (px_tn / px_t0 - 1.0) - (bx_tn / bx_t0 - 1.0)


def main() -> int:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    _check_backfill_exists(args.parquet_root)

    if args.universe_mode != "R2000":
        sys.stderr.write(
            f"ERROR: phase_a primary only supports R2000 (got {args.universe_mode}).\n"
        )
        return 4

    logger.info("Phase A check on TRAIN %s → %s", args.train_start, args.train_end)
    logger.info("Universe mode: %s", args.universe_mode)
    logger.info("Parquet root: %s", args.parquet_root)

    # Lazy imports — keep startup fast for --help.
    from alphalens.screeners.insider_activity.opportunistic_form4 import (
        aggregate_opportunistic_signal,
        score_opportunistic_form4,
    )
    from alphalens.screeners.options_implied.features import _compute_equity_controls

    # 1. PIT universe union over TRAIN window.
    universe = load_universe_union(args.train_start, args.train_end)
    if not universe:
        sys.stderr.write(
            f"ERROR: PIT universe yaml snapshots missing for {args.train_start}..{args.train_end}\n"
        )
        return 5
    logger.info("Universe union: %d tickers", len(universe))

    # 2. OHLCV histories (cached).
    histories = load_cached_histories([*universe, "IWM"], _PRICES_DIR)
    if "IWM" not in histories or histories["IWM"].empty:
        sys.stderr.write("ERROR: benchmark IWM OHLCV missing\n")
        return 6

    # 3. Stores.
    _liab, share_store = make_production_stores()
    tcm_path = REPO_ROOT / "alphalens" / "data" / "alt_data" / "data" / "ticker_cik_map.yaml"
    cik_resolver = TickerCikMap.load(tcm_path)
    form4_store = Form4PITStore(
        parquet_root=args.parquet_root,
        ticker_cik_resolver=cik_resolver,
    )
    cache = ClassifierCache(form4_store)

    # 4. Quarter-end calendar.
    quarter_ends = pd.date_range(start=args.train_start, end=args.train_end, freq="QE")
    logger.info("Evaluating %d quarter-ends", len(quarter_ends))

    # 5. For each quarter-end: compute opportunistic-txn counts (A0),
    #    scored-ticker counts (A1), and (score, fwd_21d_excess) pairs (A2).
    counts_per_quarter: list[dict[str, int]] = []
    scored_counts: list[int] = []
    score_fwd_pairs: list[tuple[float, float]] = []
    bench_history = histories["IWM"]

    for qe in quarter_ends:
        qe_date = qe.date()
        pit_universe = load_pit_universe_for_asof(qe_date)
        if not pit_universe:
            continue

        # A0: density per quarter (90d lookback).
        counts: dict[str, int] = {}
        for ticker in pit_universe:
            if ticker not in histories:
                continue
            records = form4_store.records_as_of(ticker, asof=qe_date, lookback_days=90)
            if records.empty:
                counts[ticker] = 0
                continue
            n = 0
            for row in records.itertuples(index=False):
                if row.transaction_code not in {"P", "S"}:
                    continue
                if not (row.is_officer or row.is_director):
                    continue
                if (
                    cache.get(row.reporting_owner_cik, qe_date.year)
                    is CohenMalloyLabel.OPPORTUNISTIC
                ):
                    n += 1
            counts[ticker] = n
        counts_per_quarter.append(counts)

        # A1 + A2: build feature frame, score, collect pairs.
        rows: list[dict] = []
        for ticker in pit_universe:
            if ticker not in histories:
                continue
            history = histories[ticker]
            sliced = history[history.index <= qe]
            if sliced.empty:
                continue
            controls = _compute_equity_controls(sliced)
            if controls is None:
                continue
            close = float(sliced.iloc[-1]["close"])
            if close <= 0:
                continue
            shares = share_store.get(ticker, qe)
            if not shares:
                continue
            mcap = close * shares
            if mcap <= 0:
                continue
            records = form4_store.records_as_of(ticker, asof=qe_date, lookback_days=180)
            net_oppor_usd = aggregate_opportunistic_signal(
                records, asof=qe_date, classifier_cache=cache
            )
            rows.append(
                {
                    "asof": qe_date,
                    "ticker": ticker,
                    "signal_raw": net_oppor_usd / mcap,
                    **controls,
                }
            )

        if not rows:
            scored_counts.append(0)
            continue

        feat = pd.DataFrame(rows)
        feat["score"] = score_opportunistic_form4(feat)
        scored = feat.dropna(subset=["score"])
        scored_counts.append(len(scored))

        # A2: forward 21d excess return per scored ticker.
        for _, r in scored.iterrows():
            history = histories[r["ticker"]]
            fwd = _forward_excess_return(history, bench_history, qe, horizon_days=21)
            if fwd is None or not np.isfinite(fwd):
                continue
            score_fwd_pairs.append((float(r["score"]), fwd))

    # 6. Compute three gate verdicts.
    a0 = _check_density(counts_per_quarter)
    a1 = _check_breadth(scored_counts)

    if score_fwd_pairs:
        s_arr = pd.Series([p[0] for p in score_fwd_pairs])
        f_arr = pd.Series([p[1] for p in score_fwd_pairs])
        rho = float(s_arr.corr(f_arr, method="spearman"))
        a2 = _check_direction(rho)
        a2["n_pairs"] = len(score_fwd_pairs)
    else:
        a2 = {
            "gate_id": "A2_direction",
            "spearman_rho_score_vs_fwd_21d_excess": None,
            "threshold": -0.05,
            "passed": False,
            "n_pairs": 0,
        }

    all_passed = a0["passed"] and a1["passed"] and a2["passed"]

    result = {
        "experiment": "insider_form4_opportunistic_2026_05_05",
        "phase_a_status": "evaluated",
        "train_start": args.train_start.isoformat(),
        "train_end": args.train_end.isoformat(),
        "universe_mode": args.universe_mode,
        "n_quarters": len(quarter_ends),
        "gates": [a0, a1, a2],
        "all_gates_passed": bool(all_passed),
        "verdict": "PROCEED" if all_passed else "ABANDON",
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))

    logger.info(
        "Phase A: A0_density passed=%s (median=%.2f); A1_breadth passed=%s "
        "(frac=%.2f); A2_direction passed=%s (rho=%s); verdict=%s",
        a0["passed"],
        a0.get("median_opportunistic_count_per_active_ticker_per_quarter", 0.0),
        a1["passed"],
        a1.get("frac_quarters_with_at_least_50_scored_tickers", 0.0),
        a2["passed"],
        a2.get("spearman_rho_score_vs_fwd_21d_excess"),
        result["verdict"],
    )
    return 0 if all_passed else 7


if __name__ == "__main__":
    sys.exit(main())

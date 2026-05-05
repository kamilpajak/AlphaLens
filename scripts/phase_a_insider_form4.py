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

from alphalens.data.store.form4_pit import (  # noqa: E402
    PARTITION_KEY,
    Form4PITStore,
)
from alphalens.screeners.insider_activity.cohen_malloy_classifier import (  # noqa: E402
    CohenMalloyLabel,
    classify_from_transaction_dates,
)

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


def main() -> int:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    _check_backfill_exists(args.parquet_root)

    logger.info("Phase A check on TRAIN %s → %s", args.train_start, args.train_end)
    logger.info("Universe mode: %s", args.universe_mode)
    logger.info("Parquet root: %s", args.parquet_root)

    # NOTE: Full implementation requires R2000 PIT universe loader integration
    # and OHLCV histories for forward-return computation. The orchestrator
    # script wires those in after backfill completes. This scaffold exits
    # with a clear "needs full integration" message until then.

    # Placeholder result with all gates "untested" — to be replaced with
    # real evaluation once Form-4 backfill + universe loaders are wired.
    result = {
        "experiment": "insider_form4_opportunistic_2026_05_05",
        "phase_a_status": "scaffold_only",
        "train_start": args.train_start.isoformat(),
        "train_end": args.train_end.isoformat(),
        "universe_mode": args.universe_mode,
        "gates": [
            {"gate_id": "A0_density", "passed": None, "reason": "needs backfill"},
            {"gate_id": "A1_breadth", "passed": None, "reason": "needs backfill"},
            {"gate_id": "A2_direction", "passed": None, "reason": "needs backfill"},
        ],
        "note": (
            "Scaffold only — full integration with R2000 PIT universe + "
            "Form-4 backfill required. Run after SEC EDGAR backfill completes."
        ),
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    logger.info("Wrote scaffold result to %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Pre-screen #1 + #3 for compound insider × P/C abnormal design.

Runs both component scorers on IS 2014-2017 monthly rebalance calendar.
Computes:
- Pre-screen #1: cross-sectional Spearman ρ per asof (signal independence)
- Pre-screen #3: strict-intersection ticker count per asof (coverage breadth)

Outputs verdict + decision per locked thresholds (plan
``/Users/jacoren/.claude/plans/graceful-honking-wave.md`` Phase 0.5).

NOT a production audit driver — single-shot pre-screen for compound design.
Usage:
    .venv/bin/python scripts/precheck_insider_pc_compound_independence.py
"""

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from alphalens_pipeline.data.alt_data.pit_universe_loader import load_universe_union
from alphalens_pipeline.data.alt_data.ticker_cik_map import TickerCikMap
from alphalens_pipeline.data.alt_data.yfinance_cache import load_cached_histories
from alphalens_pipeline.data.store.form4_pit import Form4PITStore
from alphalens_research.attribution.signal_independence import (
    classify_independence,
    pairwise_rank_ic_correlation,
)
from alphalens_research.screeners.distress_credit.features import (
    make_production_stores,
)
from alphalens_research.screeners.options_volume.features import build_feature_frame
from alphalens_research.screeners.options_volume.pc_abnormal_volume import (
    score_pc_abnormal_residual,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("precheck")

_PRICES_DIR = Path.home() / ".alphalens" / "prices"
_FORM4_PARQUET = Path.home() / ".alphalens" / "form4_parquet"
_SMD_DIR = Path.home() / ".alphalens" / "ivolatility_smd"
_TCM_PATH = REPO_ROOT / "alphalens_research" / "data" / "alt_data" / "data" / "ticker_cik_map.yaml"

# Decision rule constants (locked per plan)
_MIN_INTERSECTION = 5
_MIN_ASOFS = 5
_BREADTH_FLOOR = 50  # tickers per asof
_BREADTH_RATIO = 0.30  # fraction of asofs that must clear floor


def _smd_loader(ticker: str) -> pd.DataFrame | None:
    p = _SMD_DIR / f"{ticker.upper()}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    if "tradeDate" in df.columns:
        df["date"] = pd.to_datetime(df["tradeDate"])
    return df


def _build_insider_scorer():
    """Returns a callable scorer(histories, config) → DataFrame[ticker, score]."""
    from scripts.experiment_insider_form4_opportunistic import (
        ClassifierCache,
        _OpportunisticForm4Scorer,
    )

    _liab_store, share_store = make_production_stores()
    cik_resolver = TickerCikMap.load(_TCM_PATH)
    form4_store = Form4PITStore(
        parquet_root=_FORM4_PARQUET,
        ticker_cik_resolver=cik_resolver,
        delisting_events=None,
    )
    classifier_cache = ClassifierCache(form4_store)
    return _OpportunisticForm4Scorer(
        form4_store=form4_store,
        classifier_cache=classifier_cache,
        shares_store=share_store,
    )


def _monthly_asofs(start: date, end: date, *, day_of_month: int = 21) -> list[pd.Timestamp]:
    """Monthly rebalance calendar (matches insider_form4 21d stride approximation)."""
    asofs = []
    cur = pd.Timestamp(start.year, start.month, day_of_month)
    while cur.date() <= end:
        # Skip weekends (move forward to next weekday)
        while cur.weekday() > 4:
            cur += pd.Timedelta(days=1)
        if cur.date() <= end:
            asofs.append(cur)
        # Next month
        if cur.month == 12:
            cur = pd.Timestamp(cur.year + 1, 1, day_of_month)
        else:
            cur = pd.Timestamp(cur.year, cur.month + 1, day_of_month)
    return asofs


def main() -> int:
    is_start = date(2014, 1, 1)
    is_end = date(2017, 12, 31)

    logger.info("=" * 60)
    logger.info("Pre-screen IS window: %s .. %s", is_start, is_end)
    logger.info("=" * 60)

    # 1. Universe
    universe = load_universe_union(is_start, is_end)
    if not universe:
        logger.error("Empty PIT universe — abort")
        return 1
    logger.info("PIT universe: %d unique tickers", len(universe))

    # 2. OHLCV histories (subset to insider scorer needs)
    histories = load_cached_histories(universe, _PRICES_DIR)
    histories = {t: h for t, h in histories.items() if h is not None and not h.empty}
    logger.info("OHLCV histories loaded: %d tickers", len(histories))

    # 3. Asof calendar
    asofs = _monthly_asofs(is_start, is_end)
    logger.info("Monthly asofs: %d", len(asofs))

    # 4. P/C abnormal feature frame across all asofs (one shot)
    logger.info("Building P/C abnormal feature frame ...")
    pc_features = build_feature_frame(
        smd_loader=_smd_loader,
        universe=list(histories.keys()),
        asof_dates=[a.strftime("%Y-%m-%d") for a in asofs],
    )
    if pc_features.empty:
        logger.error("Empty P/C feature frame — IS data may be missing for these tickers")
        return 2
    logger.info("P/C features: %d rows", len(pc_features))
    pc_scores = score_pc_abnormal_residual(pc_features)
    pc_features = pc_features.assign(score=pc_scores).dropna(subset=["score"])
    pc_features["asof"] = pd.to_datetime(pc_features["asof"]).dt.normalize()
    pc_panel = pc_features[["asof", "ticker", "score"]].copy()
    logger.info(
        "P/C scores: %d non-NaN rows over %d asofs", len(pc_panel), pc_panel["asof"].nunique()
    )

    # 5. Insider scorer per asof
    logger.info("Running insider scorer per asof ...")
    insider_scorer = _build_insider_scorer()
    insider_rows = []
    for asof_ts in asofs:
        df = insider_scorer(histories, config={"asof": asof_ts})
        if df.empty:
            continue
        df["asof"] = asof_ts.normalize()
        insider_rows.append(df[["asof", "ticker", "score"]])
    if not insider_rows:
        logger.error("No insider scores produced — abort")
        return 3
    insider_panel = pd.concat(insider_rows, ignore_index=True)
    logger.info(
        "Insider scores: %d rows over %d asofs",
        len(insider_panel),
        insider_panel["asof"].nunique(),
    )

    # 6. Pre-screen #3 — coverage breadth (per-asof strict-intersection count)
    insider_by_asof = insider_panel.groupby("asof", observed=True)["ticker"].apply(set)
    pc_by_asof = pc_panel.groupby("asof", observed=True)["ticker"].apply(set)
    common_asofs = sorted(set(insider_by_asof.index) & set(pc_by_asof.index))
    breadth = pd.Series(
        {asof: len(insider_by_asof[asof] & pc_by_asof[asof]) for asof in common_asofs},
        name="intersection_count",
    )
    n_above = int((breadth >= _BREADTH_FLOOR).sum())
    pct_above = n_above / max(1, len(breadth))

    logger.info("=" * 60)
    logger.info("PRE-SCREEN #3 — coverage breadth")
    logger.info("=" * 60)
    logger.info("Common asofs: %d", len(breadth))
    logger.info("Per-asof intersection size — describe:")
    logger.info("\n%s", breadth.describe().to_string())
    logger.info(
        "Asofs with intersection ≥ %d: %d (%.1f%%) | threshold: ≥ %.0f%%",
        _BREADTH_FLOOR,
        n_above,
        pct_above * 100,
        _BREADTH_RATIO * 100,
    )
    breadth_pass = pct_above >= _BREADTH_RATIO
    logger.info("Pre-screen #3 verdict: %s", "PROCEED" if breadth_pass else "ABANDON")

    if not breadth_pass:
        logger.warning(
            "Coverage breadth fails on IS; OOS may be different — caveat to document in memo"
        )

    # 7. Pre-screen #1 — signal independence (cross-sectional Spearman ρ)
    logger.info("=" * 60)
    logger.info("PRE-SCREEN #1 — signal independence")
    logger.info("=" * 60)
    try:
        result = pairwise_rank_ic_correlation(
            insider_panel,
            pc_panel,
            min_intersection=_MIN_INTERSECTION,
            min_asofs=_MIN_ASOFS,
        )
    except ValueError as e:
        logger.error("Pre-screen #1 failed to compute: %s", e)
        return 4

    verdict = classify_independence(result)
    logger.info("n asofs total: %d", result.n_asofs_total)
    logger.info("n asofs with valid ρ: %d", result.n_asofs_with_valid_rho)
    logger.info("mean ρ: %+.4f", result.mean_rho)
    logger.info("t-stat: %+.3f", result.t_stat)
    logger.info("Per-asof ρ describe:")
    logger.info("\n%s", result.per_asof_rhos.dropna().describe().to_string())
    logger.info("Classification: %s", verdict.classification)
    logger.info("Proceed: %s", verdict.proceed)
    logger.info("Rationale: %s", verdict.rationale)

    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info("=" * 60)
    logger.info("Pre-screen #1 (signal independence): %s", verdict.classification)
    logger.info("Pre-screen #3 (coverage breadth IS):  %s", "PASS" if breadth_pass else "FAIL")
    if verdict.proceed and breadth_pass:
        logger.info("OVERALL: PROCEED to Phase 1 (adversarial reviews)")
    elif verdict.proceed is None:
        logger.info("OVERALL: ABORT — sign-flip suspected")
    else:
        logger.info("OVERALL: REJECT — write REJECTED memo")

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Form-4 opportunistic-insider verification gate (paradigm #11 reuse).

Wraps :func:`alphalens.screeners.insider_activity.opportunistic_form4.aggregate_opportunistic_signal`
to deliver a simple yes/no signal for the Layer 3 verification orchestrator.

Reuses paradigm #11 (gross αt +2.71 OOS validated) as a corroboration signal —
NOT as a standalone strategy. The companion ledger entry in CLAUDE.md /
project memory documents the policy: scorer reuse OK, compound architecture
NOT.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pandas as pd

from alphalens.screeners.insider_activity.cohen_malloy_classifier import (
    CohenMalloyLabel,
    classify_from_transaction_dates,
)
from alphalens.screeners.insider_activity.opportunistic_form4 import (
    aggregate_opportunistic_signal,
)

logger = logging.getLogger(__name__)

DEFAULT_FORM4_ROOT = Path.home() / ".alphalens" / "form4_parquet"
DEFAULT_LOOKBACK_DAYS = 90
DEFAULT_USD_THRESHOLD = 50_000  # $50k net opportunistic buy = meaningful


class _MemoizedClassifier:
    """Build per-(insider, year) Cohen-Malloy labels from a single history frame."""

    def __init__(self, history: pd.DataFrame):
        self._labels: dict[tuple[str, int], CohenMalloyLabel] = {}
        if history.empty:
            return
        for (cik, year), grp in history.groupby(
            ["reporting_owner_cik", history["transaction_date"].apply(lambda d: d.year)]
        ):
            self._labels[(cik, year)] = CohenMalloyLabel.UNCLASSIFIED  # placeholder
        # Build classification for each (insider, year-needed)
        years = history["transaction_date"].apply(lambda d: d.year)
        for cik, grp in history.groupby("reporting_owner_cik"):
            dates = list(grp["transaction_date"])
            for year in sorted(set(years)):
                self._labels[(cik, year)] = classify_from_transaction_dates(
                    dates, classification_year=year
                )

    def get(self, person_cik: str, year: int) -> CohenMalloyLabel:
        return self._labels.get((person_cik, year), CohenMalloyLabel.UNCLASSIFIED)


def _load_form4_for_ticker(ticker: str, *, form4_root: Path = DEFAULT_FORM4_ROOT) -> pd.DataFrame:
    """Scan the hive-partitioned form4_parquet for one ticker's rows.

    The dataset is partitioned by ``transaction_year=YYYY``. We read all
    partitions and filter; for production scale this should add a year-range
    pushdown filter via ``pyarrow.dataset``, but for ~hundreds of candidate
    tickers per day the full-scan cost is acceptable.
    """
    if not form4_root.exists():
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    for part in sorted(form4_root.glob("transaction_year=*/compacted.parquet")):
        df = pd.read_parquet(part)
        if df.empty:
            continue
        df = df[df["ticker"] == ticker.upper()]
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def filter_records(
    records: pd.DataFrame,
    *,
    ticker: str,
    asof: dt.date,
    lookback_days: int,
) -> pd.DataFrame:
    """Restrict ``records`` to ``ticker`` within ``[asof - lookback, asof]``."""
    if records.empty:
        return records
    cutoff = asof - dt.timedelta(days=lookback_days)
    mask = (
        (records["ticker"] == ticker.upper())
        & (records["transaction_date"] >= cutoff)
        & (records["transaction_date"] <= asof)
    )
    return records[mask].reset_index(drop=True)


def has_opportunistic_buy(
    *,
    ticker: str,
    asof: dt.date,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    usd_threshold: float = DEFAULT_USD_THRESHOLD,
    form4_root: Path = DEFAULT_FORM4_ROOT,
) -> bool:
    """Layer 3 verification gate: net opportunistic insider buy over threshold?"""
    try:
        history = _load_form4_for_ticker(ticker, form4_root=form4_root)
    except Exception as exc:
        logger.warning("form4 load failed for %s: %s", ticker, exc)
        return False
    if history.empty:
        return False

    classifier_cache = _MemoizedClassifier(history)
    recent = filter_records(history, ticker=ticker, asof=asof, lookback_days=lookback_days)
    if recent.empty:
        return False

    net_usd = aggregate_opportunistic_signal(recent, asof=asof, classifier_cache=classifier_cache)
    return net_usd >= usd_threshold


__all__ = [
    "DEFAULT_FORM4_ROOT",
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_USD_THRESHOLD",
    "filter_records",
    "has_opportunistic_buy",
]

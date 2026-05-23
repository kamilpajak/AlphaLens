"""Form-4 opportunistic-insider verification gate (paradigm #11 reuse).

Wraps :func:`alphalens_pipeline.scorers.opportunistic_form4.aggregate_opportunistic_signal`
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

from alphalens_pipeline.scorers.cohen_malloy_classifier import (
    CohenMalloyLabel,
    classify_from_transaction_dates,
)
from alphalens_pipeline.scorers.opportunistic_form4 import (
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
        years = sorted({d.year for d in history["transaction_date"]})
        for cik, grp in history.groupby("reporting_owner_cik"):
            dates = list(grp["transaction_date"])
            for year in years:
                self._labels[(str(cik), int(year))] = classify_from_transaction_dates(
                    dates, classification_year=year
                )

    def get(self, person_cik: str, classification_year: int) -> CohenMalloyLabel:
        return self._labels.get((person_cik, classification_year), CohenMalloyLabel.UNCLASSIFIED)


def _classification_years(asof: dt.date, *, lookback_classification_years: int = 3) -> set[int]:
    """Years Cohen-Malloy needs visible to classify trades at ``asof``."""
    return {asof.year - i for i in range(lookback_classification_years + 1)}


def _load_form4_partitions(
    *,
    form4_root: Path,
    years: set[int],
    ticker: str | None = None,
    insider_ciks: set[str] | None = None,
) -> pd.DataFrame:
    """Read only the partitions in ``years``; optionally narrow by ticker / insider.

    The hive layout is ``transaction_year=YYYY/compacted.parquet``; pruning to
    the classification window avoids scanning 30+ years of history just to
    decide one 4-year question.
    """
    if not form4_root.exists() or not years:
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    for year in sorted(years):
        part = form4_root / f"transaction_year={year}" / "compacted.parquet"
        if not part.exists():
            continue
        df = pd.read_parquet(part)
        if df.empty:
            continue
        if ticker is not None:
            df = df[df["ticker"] == ticker.upper()]
        if insider_ciks is not None:
            df = df[df["reporting_owner_cik"].isin(insider_ciks)]
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _load_form4_for_ticker(
    ticker: str,
    *,
    form4_root: Path = DEFAULT_FORM4_ROOT,
    years: set[int] | None = None,
) -> pd.DataFrame:
    """Ticker-restricted Form-4 slice.

    NOTE — this slice is suitable only for identifying ACTIVE insiders or for
    aggregating the final ticker-specific signal. Cohen-Malloy classification
    MUST run over the insider's full cross-ticker history; for that, call
    :func:`_load_form4_for_insiders`.
    """
    if years is None:
        # Backward-compatible callers: scan everything (legacy slow path).
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
    return _load_form4_partitions(form4_root=form4_root, years=years, ticker=ticker)


def _load_form4_for_insiders(
    insider_ciks: set[str],
    *,
    form4_root: Path = DEFAULT_FORM4_ROOT,
    years: set[int],
) -> pd.DataFrame:
    """Cross-ticker history for ``insider_ciks`` restricted to ``years``."""
    return _load_form4_partitions(form4_root=form4_root, years=years, insider_ciks=insider_ciks)


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
) -> bool | None:
    """Layer 3 verification gate: net opportunistic insider buy over threshold?

    Tri-state: ``True`` (net buy ≥ threshold), ``False`` (window had trades
    but didn't qualify — sold, routine class, or below threshold), ``None``
    (no Form-4 data available for ticker, or loader exception — orchestrator
    records as unknown, NOT a false negative).

    Two-step Form-4 load so Cohen-Malloy sees each insider's FULL cross-ticker
    history. A March-every-year trader is ROUTINE regardless of WHICH ticker
    they touch; a ticker-restricted view would mislabel them as opportunistic
    on whichever ticker first breaks the pattern.
    """
    years = _classification_years(asof)
    try:
        ticker_history = _load_form4_for_ticker(ticker, form4_root=form4_root, years=years)
    except Exception as exc:
        logger.warning("form4 load failed for %s: %s", ticker, exc)
        return None
    if ticker_history.empty:
        # No data anywhere for this ticker — distinct from "data present but
        # window/threshold rejected". Surface as unknown so the operator can
        # tell the difference downstream.
        return None

    recent = filter_records(ticker_history, ticker=ticker, asof=asof, lookback_days=lookback_days)
    if recent.empty:
        # Ticker has Form-4 history but no trades in the lookback window —
        # that IS a real "no recent insider activity" signal, not missing data.
        return False

    active_insiders = set(recent["reporting_owner_cik"].dropna().astype(str))
    try:
        full_history = _load_form4_for_insiders(active_insiders, form4_root=form4_root, years=years)
    except Exception as exc:
        logger.warning("form4 cross-ticker load failed for %s: %s", ticker, exc)
        return None
    if full_history.empty:
        # Degrade gracefully — at least classify on the visible trades.
        full_history = ticker_history

    classifier_cache = _MemoizedClassifier(full_history)
    net_usd = aggregate_opportunistic_signal(recent, asof=asof, classifier_cache=classifier_cache)
    return net_usd >= usd_threshold


__all__ = [
    "DEFAULT_FORM4_ROOT",
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_USD_THRESHOLD",
    "filter_records",
    "has_opportunistic_buy",
]

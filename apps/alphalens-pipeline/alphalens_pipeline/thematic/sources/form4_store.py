"""Shared Form-4 load + Cohen-Malloy classification primitives.

Neutral home for the Form-4 parquet readers, the per-(insider, year)
Cohen-Malloy memoized classifier, the classification-year window helper, and
the ticker/window record filter. Both the Layer 3 verification gate
(``verification.insider``) and the Layer 4 screening signal
(``screening.insider_signal``) import from here so neither stage reaches into
the other's private internals (see ``screening/_common.py`` for the same
"shared primitives live in one neutral module" rationale).

These names are the PUBLIC shared API — they carry no leading underscore.
The hive-partitioned source is ``~/.alphalens/form4_parquet/`` laid out as
``transaction_year=YYYY/compacted.parquet`` (the VPS Form-4 backfill output).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

from alphalens_pipeline.scorers.cohen_malloy_classifier import (
    CohenMalloyLabel,
    classify_from_transaction_dates,
)

DEFAULT_FORM4_ROOT = Path.home() / ".alphalens" / "form4_parquet"
DEFAULT_LOOKBACK_DAYS = 90


class MemoizedClassifier:
    """Build per-(insider, year) Cohen-Malloy labels from a single history frame."""

    def __init__(self, history: pd.DataFrame):
        self._labels: dict[tuple[str, int], CohenMalloyLabel] = {}
        if history.empty:
            return
        # Legacy parquet written before the F1/F2 year guards may carry NaT
        # transaction_date or NULL reporting_owner_cik. A NaT in the year set
        # makes {d.year for d in ...} yield nan -> int(nan) raises and takes
        # down the whole ticker; a NULL cik creates an unreachable ('nan', year)
        # group. Build the classifier only from usable rows.
        history = history.dropna(subset=["transaction_date", "reporting_owner_cik"])
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


def classification_years(asof: dt.date, *, lookback_classification_years: int = 3) -> set[int]:
    """Years Cohen-Malloy needs visible to classify trades at ``asof``."""
    return {asof.year - i for i in range(lookback_classification_years + 1)}


def load_form4_partitions(
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


def load_form4_for_ticker(
    ticker: str,
    *,
    form4_root: Path = DEFAULT_FORM4_ROOT,
    years: set[int] | None = None,
) -> pd.DataFrame:
    """Ticker-restricted Form-4 slice.

    NOTE — this slice is suitable only for identifying ACTIVE insiders or for
    aggregating the final ticker-specific signal. Cohen-Malloy classification
    MUST run over the insider's full cross-ticker history; for that, call
    :func:`load_form4_for_insiders`.
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
    return load_form4_partitions(form4_root=form4_root, years=years, ticker=ticker)


def load_form4_for_insiders(
    insider_ciks: set[str],
    *,
    form4_root: Path = DEFAULT_FORM4_ROOT,
    years: set[int],
) -> pd.DataFrame:
    """Cross-ticker history for ``insider_ciks`` restricted to ``years``."""
    return load_form4_partitions(form4_root=form4_root, years=years, insider_ciks=insider_ciks)


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


__all__ = [
    "DEFAULT_FORM4_ROOT",
    "DEFAULT_LOOKBACK_DAYS",
    "MemoizedClassifier",
    "classification_years",
    "filter_records",
    "load_form4_for_insiders",
    "load_form4_for_ticker",
    "load_form4_partitions",
]

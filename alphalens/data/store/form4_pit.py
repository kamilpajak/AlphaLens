"""Point-in-time Form-4 record store.

Reads a hive-partitioned parquet dataset (partitioned by ``transaction_year``)
and exposes two PIT-aware query patterns:

  * :meth:`Form4PITStore.records_as_of` — records used by the signal scorer
    (filtered by issuer_cik resolved from ticker). Filters by ``filed_date <=
    asof`` and ``transaction_date >= asof - lookback_days``. Applies 180d
    fire-sale exclusion via :class:`DelistingEvent` (per PIT audit F4 finding,
    100-300 bps inflation w/o fix).

  * :meth:`Form4PITStore.records_for_person` — records used by the
    Cohen-Malloy classifier (filtered by reporting_owner_cik over the rolling
    [classification_year - 3, classification_year) window).

Schema is locked via :data:`FORM4_SCHEMA_COLUMNS`; the bulk-backfill writer
must produce parquet files matching exactly these columns.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

import pandas as pd
import pyarrow.dataset as ds

from alphalens.data.store.survivorship_pit import DelistingEvent

# Locked schema. Order matters for parquet writer parity tests.
FORM4_SCHEMA_COLUMNS: tuple[str, ...] = (
    "issuer_cik",
    "ticker",
    "accession_number",
    "filed_date",
    "reporting_owner_cik",
    "reporting_owner_name",
    "transaction_date",
    "transaction_code",
    "transaction_shares",
    "transaction_price_per_share",
    "is_director",
    "is_officer",
    "is_ten_percent_owner",
    "acquired_disposed",
    "is_amendment",
)

DEFAULT_DELISTING_EXCLUSION_DAYS: int = 180
PARTITION_KEY: str = "transaction_year"


class _CikResolver(Protocol):
    def lookup(self, ticker: str) -> str | None: ...


class Form4PITStore:
    """Hive-partitioned Form-4 record store with PIT discipline."""

    def __init__(
        self,
        parquet_root: Path,
        *,
        delisting_events: Iterable[DelistingEvent] | None = None,
        delisting_exclusion_days: int = DEFAULT_DELISTING_EXCLUSION_DAYS,
        ticker_cik_resolver: _CikResolver | None = None,
    ):
        self._root = Path(parquet_root)
        self._resolver = ticker_cik_resolver
        self._delisting_exclusion_days = int(delisting_exclusion_days)

        delisting_by_ticker: dict[str, list[date]] = {}
        if delisting_events is not None:
            for ev in delisting_events:
                delisting_by_ticker.setdefault(ev.ticker.upper(), []).append(ev.delisted_date)
        self._delisting_by_ticker = delisting_by_ticker

    @property
    def parquet_root(self) -> Path:
        return self._root

    @property
    def ticker_cik_resolver(self) -> _CikResolver | None:
        return self._resolver

    def records_as_of(
        self,
        ticker: str,
        asof: date,
        lookback_days: int = DEFAULT_DELISTING_EXCLUSION_DAYS,
    ) -> pd.DataFrame:
        """Return records for ``ticker`` with PIT discipline.

        Filters:
          * ``filed_date <= asof`` (no peeking at filings unfiled at asof)
          * ``transaction_date >= asof - lookback_days`` (signal window)
          * If ``ticker`` delists within ``delisting_exclusion_days`` after
            ``asof``, returns an empty DataFrame (fire-sale exclusion).
        """
        ticker_up = ticker.upper()
        if self._is_delisting_within_window(ticker_up, asof):
            return self._empty_frame()

        if self._resolver is None:
            return self._empty_frame()
        issuer_cik = self._resolver.lookup(ticker_up)
        if issuer_cik is None:
            return self._empty_frame()

        lookback_start = asof - timedelta(days=int(lookback_days))
        years = self._year_range(lookback_start.year, asof.year)
        df = self._load_partitions(years)
        if df.empty:
            return df

        return df.loc[
            (df["issuer_cik"] == issuer_cik)
            & (df["filed_date"] <= asof)
            & (df["transaction_date"] >= lookback_start)
        ].reset_index(drop=True)

    def records_for_person(
        self,
        person_cik: str,
        classification_year: int,
    ) -> pd.DataFrame:
        """Return records by ``person_cik`` in the [Y-3, Y) classification window.

        Filters by ``transaction_date.year`` falling in the 3-year lookback
        window strictly before ``classification_year``. Used by the
        Cohen-Malloy classifier per paper p. 1786.
        """
        years = list(range(classification_year - 3, classification_year))
        df = self._load_partitions(years)
        if df.empty:
            return df

        # transaction_date.year must lie within window — partition column already
        # narrows but post-filter for safety against edge-of-partition records.
        years_set = set(years)
        df = df.loc[
            (df["reporting_owner_cik"] == person_cik)
            & (df["transaction_date"].apply(lambda d: d.year in years_set))
        ].reset_index(drop=True)
        return df

    def _is_delisting_within_window(self, ticker: str, asof: date) -> bool:
        if self._delisting_exclusion_days <= 0:
            return False
        cutoff = asof + timedelta(days=self._delisting_exclusion_days)
        return any(d <= cutoff for d in self._delisting_by_ticker.get(ticker, []))

    def _year_range(self, start_year: int, end_year: int) -> list[int]:
        return list(range(start_year, end_year + 1))

    def _load_partitions(self, years: Iterable[int]) -> pd.DataFrame:
        existing_dirs = [
            self._root / f"{PARTITION_KEY}={y}"
            for y in years
            if (self._root / f"{PARTITION_KEY}={y}").is_dir()
        ]
        if not existing_dirs:
            return self._empty_frame()
        # Read each partition independently and concat — pyarrow handles hive
        # partition columns automatically but raises on missing dirs.
        frames = []
        for d in existing_dirs:
            dataset = ds.dataset(str(d), partitioning=None, format="parquet")
            table = dataset.to_table(columns=list(FORM4_SCHEMA_COLUMNS))
            frames.append(table.to_pandas())
        if not frames:
            return self._empty_frame()
        df = pd.concat(frames, ignore_index=True)
        # Normalise date columns (parquet may surface as Timestamp).
        for col in ("filed_date", "transaction_date"):
            if col in df.columns:
                df[col] = df[col].apply(_to_date)
        return df

    @staticmethod
    def _empty_frame() -> pd.DataFrame:
        return pd.DataFrame(columns=list(FORM4_SCHEMA_COLUMNS))


def _to_date(value: object) -> date:
    if isinstance(value, date) and not hasattr(value, "to_pydatetime"):
        return value
    if hasattr(value, "date"):
        return value.date()  # type: ignore[no-any-return]
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError(f"unsupported date cell type: {type(value).__name__}")

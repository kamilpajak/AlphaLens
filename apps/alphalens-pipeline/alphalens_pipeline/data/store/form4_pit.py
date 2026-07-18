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

import logging
from collections import OrderedDict
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

import pandas as pd
import pyarrow.dataset as ds

from alphalens_pipeline.data.store.delisting import DelistingEvent

logger = logging.getLogger(__name__)

# Default LRU capacity. 32 covers the largest realistic working set:
# a 6yr lookback × 5 audit phases × concurrent OOS+final-lock ≈ 18 unique
# years. Override via Form4PITStore(..., partition_cache_size=N).
DEFAULT_PARTITION_CACHE_SIZE: int = 32

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
        partition_cache_size: int = DEFAULT_PARTITION_CACHE_SIZE,
    ):
        self._root = Path(parquet_root)
        self._resolver = ticker_cik_resolver
        self._delisting_exclusion_days = int(delisting_exclusion_days)

        delisting_by_ticker: dict[str, list[date]] = {}
        if delisting_events is not None:
            for ev in delisting_events:
                delisting_by_ticker.setdefault(ev.ticker.upper(), []).append(ev.delisted_date)
        self._delisting_by_ticker = delisting_by_ticker

        # Per-year partition cache. On RunPod's MooseFS network volume,
        # `records_as_of` was observed reloading the same year partition
        # ~1800x per audit phase (~38 GB rchar). Cache makes each year a
        # one-shot parquet read for the lifetime of the store instance.
        # Per-process: workers don't share state, which is fine — the
        # backtest engine is single-threaded per ticker batch.
        self._partition_cache_size = int(partition_cache_size)
        self._partition_cache: OrderedDict[int, pd.DataFrame] = OrderedDict()

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
            # Expected PIT rule (fire-sale exclusion), not an anomaly — DEBUG.
            logger.debug(
                "records_as_of empty: %s excluded by fire-sale rule at %s", ticker_up, asof
            )
            return self._empty_frame()

        # Distinguish the empty paths so 'no data' and a resolver failure are
        # not silently indistinguishable at the call site. A resolver failure
        # means a universe member's insider signal is silently absent — worth a
        # WARNING; a genuine no-records-in-window is expected — DEBUG below.
        if self._resolver is None:
            logger.warning(
                "records_as_of empty: no ticker_cik_resolver configured; cannot resolve %s",
                ticker_up,
            )
            return self._empty_frame()
        issuer_cik = self._resolver.lookup(ticker_up)
        if issuer_cik is None:
            logger.warning(
                "records_as_of empty: ticker %s not resolvable to a CIK (asof %s)",
                ticker_up,
                asof,
            )
            return self._empty_frame()

        lookback_start = asof - timedelta(days=int(lookback_days))
        years = self._year_range(lookback_start.year, asof.year)

        # Filter-before-concat: applying the boolean mask per yearly frame
        # before stitching them together avoids materialising a multi-year
        # full concat (~232k rows × 1800 calls/phase = ~417M row-allocs of
        # churn). `.loc[mask]` returns a fresh frame, so cache integrity
        # is preserved without `copy=True` on the concat.
        per_year_filtered: list[pd.DataFrame] = []
        for y in years:
            df = self._load_one_year(int(y))
            if df is None or df.empty:
                continue
            mask = (
                (df["issuer_cik"] == issuer_cik)
                & (df["filed_date"] <= asof)
                & (df["transaction_date"] >= lookback_start)
            )
            sub = df.loc[mask]
            if not sub.empty:
                per_year_filtered.append(sub)

        if not per_year_filtered:
            # Resolver worked but no records fall in the window — expected and
            # common across a broad universe, so DEBUG (not WARNING) to avoid
            # audit-log spam.
            logger.debug(
                "records_as_of empty: no records for %s (cik %s) in window [%s, %s]",
                ticker_up,
                issuer_cik,
                lookback_start,
                asof,
            )
            return self._empty_frame()
        result = pd.concat(per_year_filtered, ignore_index=True).reset_index(drop=True)
        # Cross-check the resolved records against the requested ticker. A stale
        # ticker->CIK map returns another issuer's records with zero error; the
        # filter above keys only on issuer_cik, so the parquet ``ticker`` column
        # is the independent witness. Warn WITHOUT altering the returned data —
        # this is a pinned-audit path; observability, never a silent drop.
        result_tickers = set(result["ticker"].dropna().astype(str).str.upper())
        if result_tickers and ticker_up not in result_tickers:
            logger.warning(
                "records_as_of possible CIK misresolution (or ticker change): "
                "requested %s (cik %s) but records carry %s",
                ticker_up,
                issuer_cik,
                sorted(result_tickers),
            )
        return result

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
        years_set = set(years)

        # Filter-before-concat: see records_as_of for rationale. Applying
        # the per-year mask first keeps allocations proportional to the
        # subject-person's activity, not the cross-section's.
        per_year_filtered: list[pd.DataFrame] = []
        for y in years:
            df = self._load_one_year(int(y))
            if df is None or df.empty:
                continue
            mask = (df["reporting_owner_cik"] == person_cik) & (
                df["transaction_date"].apply(lambda d: d.year in years_set)
            )
            sub = df.loc[mask]
            if not sub.empty:
                per_year_filtered.append(sub)

        if not per_year_filtered:
            return self._empty_frame()
        return pd.concat(per_year_filtered, ignore_index=True).reset_index(drop=True)

    def _is_delisting_within_window(self, ticker: str, asof: date) -> bool:
        if self._delisting_exclusion_days <= 0:
            return False
        cutoff = asof + timedelta(days=self._delisting_exclusion_days)
        return any(d <= cutoff for d in self._delisting_by_ticker.get(ticker, []))

    def _year_range(self, start_year: int, end_year: int) -> list[int]:
        return list(range(start_year, end_year + 1))

    def _load_one_year(self, year: int) -> pd.DataFrame | None:
        """Return the normalised parquet partition for ``year`` (or None).

        Each year is parquet-loaded at most once per store instance.
        Returns None for years whose partition directory does not exist;
        these are NOT cached so a later partition write becomes visible.
        """
        cached = self._partition_cache.get(year)
        if cached is not None:
            self._partition_cache.move_to_end(year)
            return cached

        part_dir = self._root / f"{PARTITION_KEY}={year}"
        if not part_dir.is_dir():
            return None

        dataset = ds.dataset(str(part_dir), partitioning=None, format="parquet")
        table = dataset.to_table(columns=list(FORM4_SCHEMA_COLUMNS))
        df = table.to_pandas()
        # Normalise date columns once at cache fill (parquet may surface
        # them as pd.Timestamp); cache hits then skip the per-row apply.
        for col in ("filed_date", "transaction_date"):
            if col in df.columns:
                df[col] = df[col].apply(_to_date)

        # OrderedDict insertion already places the key at the MRU end,
        # so no explicit move_to_end is needed here.
        self._partition_cache[year] = df
        while len(self._partition_cache) > self._partition_cache_size:
            self._partition_cache.popitem(last=False)
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

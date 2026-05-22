"""PIT feature joiners for distress_credit scorer.

Stores wrap PIT lookups for total liabilities and shares outstanding.
Scorer adapter assembles (mcap, liabilities, sigma_60d, rf_1y) per ticker
per asof. Read-side abstractions — actual companyfacts parquet reads
happen via ``CompanyfactsParquetReader`` from
``alphalens_research.data.fundamentals.companyfacts_parquet``.

Survivorship caveat: SP1500 PIT loader has sparse-snapshot bias (~100-300
bps/y), asymmetrically favoring safe names (winners-only set). Documented
in pre-reg memo; flagged in verdict.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MertonInputs:
    """One row of joined inputs for Merton DD computation at a single asof."""

    ticker: str
    asof: pd.Timestamp
    equity_mcap: float
    total_liabilities: float
    sigma_equity_ann: float
    rf_1y: float


class LiabilitiesStoreProtocol:
    """Read-side protocol for PIT total liabilities lookup.

    Real implementation queries ``us-gaap:Liabilities`` from companyfacts
    parquet at ``~/.alphalens/companyfacts_parquet/`` filtered by
    ``filed_date <= asof`` (latest filed per period_end). Tests use
    ``InMemoryLiabilitiesStore``.
    """

    def get(self, ticker: str, asof: pd.Timestamp) -> float | None:
        raise NotImplementedError


class ShareCountStoreProtocol:
    """Read-side protocol for PIT shares-outstanding lookup.

    Real implementation queries ``us-gaap:CommonStockSharesOutstanding``
    from companyfacts parquet at ``filed_date <= asof``. Tests use
    ``InMemoryShareCountStore``.
    """

    def get(self, ticker: str, asof: pd.Timestamp) -> float | None:
        raise NotImplementedError


class InMemoryLiabilitiesStore(LiabilitiesStoreProtocol):
    """Test fixture: ticker -> constant liabilities (no time variation)."""

    def __init__(self, mapping: Mapping[str, float]):
        self._m = dict(mapping)

    def get(self, ticker: str, asof: pd.Timestamp) -> float | None:
        return self._m.get(ticker)


class InMemoryShareCountStore(ShareCountStoreProtocol):
    """Test fixture: ticker -> constant shares (no time variation)."""

    def __init__(self, mapping: Mapping[str, float]):
        self._m = dict(mapping)

    def get(self, ticker: str, asof: pd.Timestamp) -> float | None:
        return self._m.get(ticker)


# ---------------------------------------------------------------------------
# Production stores — read companyfacts parquet, PIT filter via filed_date


class CompanyfactsLiabilitiesStore(LiabilitiesStoreProtocol):
    """PIT total liabilities (us-gaap:Liabilities, USD) from companyfacts parquet.

    For ticker → CIK → parquet table → filter (taxonomy='us-gaap',
    concept='Liabilities', unit='USD') → keep entries with filed_date <= asof
    → return latest by (period_end, filed_date).

    Returns ``None`` for unknown ticker, missing parquet, no Liabilities tag,
    or no entries visible at asof.
    """

    def __init__(
        self,
        *,
        ticker_cik_map,
        reader,
    ):
        self._tcm = ticker_cik_map
        self._reader = reader

    def get(self, ticker: str, asof: pd.Timestamp) -> float | None:
        from alphalens_research.data.fundamentals.companyfacts_parquet import filter_concept

        cik = self._tcm.lookup(ticker)
        if cik is None:
            return None
        table = self._reader.get_cik_table(cik)
        if table is None or table.num_rows == 0:
            return None
        liab_table = filter_concept(table, "us-gaap", "Liabilities", unit="USD")
        if liab_table.num_rows == 0:
            return None
        asof_date = asof.date() if isinstance(asof, pd.Timestamp) else asof
        rows = liab_table.to_pylist()
        eligible = [r for r in rows if r["filed_date"] <= asof_date]
        if not eligible:
            return None
        latest = max(eligible, key=lambda r: (r["period_end"], r["filed_date"]))
        val = float(latest["val"])
        if val <= 0 or pd.isna(val):
            return None
        return val


class CompanyfactsShareCountStore(ShareCountStoreProtocol):
    """PIT shares outstanding from companyfacts parquet.

    Tries us-gaap:CommonStockSharesOutstanding first, falls back to
    dei:EntityCommonStockSharesOutstanding.
    """

    _CANDIDATES = (
        ("us-gaap", "CommonStockSharesOutstanding"),
        ("dei", "EntityCommonStockSharesOutstanding"),
    )

    def __init__(
        self,
        *,
        ticker_cik_map,
        reader,
    ):
        self._tcm = ticker_cik_map
        self._reader = reader

    def get(self, ticker: str, asof: pd.Timestamp) -> float | None:
        from alphalens_research.data.fundamentals.companyfacts_parquet import filter_concept

        cik = self._tcm.lookup(ticker)
        if cik is None:
            return None
        table = self._reader.get_cik_table(cik)
        if table is None or table.num_rows == 0:
            return None
        asof_date = asof.date() if isinstance(asof, pd.Timestamp) else asof
        for taxonomy, concept in self._CANDIDATES:
            sub = filter_concept(table, taxonomy, concept, unit="shares")
            if sub.num_rows == 0:
                continue
            rows = sub.to_pylist()
            eligible = [r for r in rows if r["filed_date"] <= asof_date]
            if not eligible:
                continue
            latest = max(eligible, key=lambda r: (r["period_end"], r["filed_date"]))
            val = float(latest["val"])
            if val > 0:
                return val
        return None


def make_production_stores(
    *, parquet_dir: Path | None = None, ticker_cik_map_path: Path | None = None
) -> tuple[CompanyfactsLiabilitiesStore, CompanyfactsShareCountStore]:
    """Wire the two production stores against the canonical local caches."""
    from alphalens_research.data.alt_data.ticker_cik_map import TickerCikMap
    from alphalens_research.data.fundamentals.companyfacts_parquet import (
        CompanyfactsParquetReader,
    )

    parquet_dir = parquet_dir or Path.home() / ".alphalens" / "companyfacts_parquet"
    ticker_cik_map_path = (
        ticker_cik_map_path
        or Path(__file__).resolve().parents[2]
        / "data"
        / "alt_data"
        / "data"
        / "ticker_cik_map.yaml"
    )
    tcm = TickerCikMap.load(ticker_cik_map_path)
    reader = CompanyfactsParquetReader(parquet_dir)
    liab = CompanyfactsLiabilitiesStore(ticker_cik_map=tcm, reader=reader)
    shares = CompanyfactsShareCountStore(ticker_cik_map=tcm, reader=reader)
    return liab, shares

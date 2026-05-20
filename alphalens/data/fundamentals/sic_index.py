"""Ticker / industry / sector resolver backed by SEC's SIC codes.

Replaces the former SimFin bulk-metadata loader at
:mod:`alphalens.thematic.screening.sector_peers` after PR #161 removed the
``simfin>=1.0.2`` dependency but missed this independent SimFin consumer
(issue #169).

Source of truth: a small parquet shipped at
``alphalens/data/fundamentals/sic_index.parquet`` with columns
``ticker / cik / sic / sic_description``. Rebuilt offline by
``scripts/build_sic_index.py`` (walks the SP1500 PIT YAMLs + delisted
overlay, fetches each CIK's top-level ``sic``/``sicDescription`` via the
canonical :class:`alphalens.data.alt_data.sec_edgar_client.SecEdgarClient`,
writes the parquet). Refresh cadence: manual, monthly — SIC reassignments
are rare.

Cohort-width note: SIC's 4-digit taxonomy is broader than SimFin's
6-digit hierarchical IndustryId. The legacy "Quantum Computing"
sub-industry (4 tickers) is absorbed into "Semiconductors & Related
Devices" (~100 tickers). Sector-percentile signals consumed by
``scorer.py`` widen accordingly. Acceptable trade-off for the
single-vendor (EDGAR) unblock; theme-conditional cohort refinement is
deferred.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

# Default artifact location: package-internal so it ships in the Docker
# pipeline image (`deploy/docker/Dockerfile.pipeline` `COPY`s `alphalens/`).
# Test code monkey-patches this module attribute.
_SIC_INDEX_PATH = Path(__file__).parent / "sic_index.parquet"


# SIC Division ranges per SEC's published Standard Industrial Classification
# manual. Each tuple is (low_inclusive, high_inclusive, division_name).
# Source: https://www.sec.gov/info/edgar/siccodes.htm
_SIC_DIVISION_RANGES: tuple[tuple[int, int, str], ...] = (
    (100, 999, "Agriculture, Forestry and Fishing"),
    (1000, 1499, "Mining"),
    (1500, 1799, "Construction"),
    (2000, 3999, "Manufacturing"),
    (4000, 4999, "Transportation, Communications, Electric, Gas and Sanitary services"),
    (5000, 5199, "Wholesale Trade"),
    (5200, 5999, "Retail Trade"),
    (6000, 6799, "Finance, Insurance and Real Estate"),
    (7000, 8999, "Services"),
    (9100, 9729, "Public Administration"),
)


def _division_name(sic: int) -> str | None:
    """Return the SEC division name for a 4-digit SIC code, or None if unmapped."""
    for low, high, name in _SIC_DIVISION_RANGES:
        if low <= sic <= high:
            return name
    return None


@lru_cache(maxsize=1)
def _load_index() -> pa.Table | None:
    """Read the SIC index parquet once per process; None when missing."""
    if not _SIC_INDEX_PATH.exists():
        return None
    return pq.read_table(_SIC_INDEX_PATH)


def get_sic(ticker: str) -> int | None:
    """Return the 4-digit SEC SIC code for ``ticker``, or None if unmapped.

    Case-insensitive on the ticker. Missing ticker, missing index file, or a
    ticker with a null SIC all resolve to None — the caller's contract
    treats None as "no peer cohort available" and skips the percentile
    signal for that candidate.
    """
    if not ticker:
        return None
    table = _load_index()
    if table is None:
        return None
    upper = ticker.upper()
    tickers = table.column("ticker").to_pylist()
    try:
        idx = tickers.index(upper)
    except ValueError:
        return None
    value = table.column("sic")[idx].as_py()
    if value is None:
        return None
    return int(value)


def iter_sic_peers(sic: int | None) -> list[str]:
    """Return all tickers sharing ``sic``. Empty list for unknown / None.

    Membership is computed from the same parquet artifact, so the peer set
    reflects whichever PIT universe the index was built from. New IPOs that
    were absent at build time will not appear as peers until the next
    ``scripts/build_sic_index.py`` refresh.
    """
    if sic is None:
        return []
    table = _load_index()
    if table is None:
        return []
    tickers = table.column("ticker").to_pylist()
    sics = table.column("sic").to_pylist()
    return [t for t, s in zip(tickers, sics, strict=True) if s == sic]


def sic_label(sic: int | None) -> tuple[str | None, str | None]:
    """Return ``(industry_name, sector_name)`` for ``sic``.

    ``industry_name`` is the EDGAR-reported ``sicDescription`` (the
    fine-grained 4-digit-code human label). ``sector_name`` is the SEC
    SIC Division name (coarser, 10 buckets). ``(None, None)`` when the
    SIC is unknown to the index.
    """
    if sic is None:
        return (None, None)
    table = _load_index()
    if table is None:
        return (None, None)
    sics = table.column("sic").to_pylist()
    try:
        idx = sics.index(sic)
    except ValueError:
        return (None, None)
    description = table.column("sic_description")[idx].as_py()
    return (description, _division_name(sic))


__all__ = ["get_sic", "iter_sic_peers", "sic_label"]

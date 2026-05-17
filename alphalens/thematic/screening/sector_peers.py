"""SimFin sector/industry resolver used by Phase D cohort-percentile signals.

Reads ``~/.alphalens/simfin_cache/{us-companies,industries}.csv`` (SimFin's
free bulk metadata) and exposes:

- :func:`get_industry_id` — ticker → SimFin IndustryId
- :func:`iter_industry_peers` — IndustryId → list of peer tickers
- :func:`industry_label` — IndustryId → (industry_name, sector_name)

CSV reads are :func:`functools.lru_cache`-d at module load. All four pure-play
quantum tickers (IONQ, QUBT, RGTI, QBTS) resolve to IndustryId=101001 in the
current SimFin snapshot, so industry-cohort ranking is feasible without
cross-sector contamination.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import pandas as pd

SIMFIN_CACHE_DIR = Path(
    os.environ.get("SIMFIN_DATA_DIR") or Path.home() / ".alphalens" / "simfin_cache"
)


@lru_cache(maxsize=1)
def _load_companies() -> pd.DataFrame:
    path = SIMFIN_CACHE_DIR / "us-companies.csv"
    df = pd.read_csv(path, sep=";", dtype={"Ticker": str, "IndustryId": "Int64"})
    # SimFin CSV has rows with missing tickers (private/delisted entries); drop them.
    df = df.dropna(subset=["Ticker", "IndustryId"])
    df["Ticker"] = df["Ticker"].str.upper()
    return df


@lru_cache(maxsize=1)
def _load_industries() -> pd.DataFrame:
    path = SIMFIN_CACHE_DIR / "industries.csv"
    return pd.read_csv(path, sep=";", dtype={"IndustryId": "Int64"})


def get_industry_id(ticker: str) -> int | None:
    """Return SimFin IndustryId for ``ticker``, or ``None`` if unmapped."""
    df = _load_companies()
    hit = df[df["Ticker"] == ticker.upper()]
    if hit.empty:
        return None
    value = hit.iloc[0]["IndustryId"]
    if pd.isna(value):
        return None
    return int(value)


def iter_industry_peers(industry_id: int) -> list[str]:
    """Return all tickers sharing ``industry_id``. Empty list if unknown."""
    df = _load_companies()
    return df.loc[df["IndustryId"] == industry_id, "Ticker"].tolist()


def industry_label(industry_id: int) -> tuple[str | None, str | None]:
    """Return ``(industry_name, sector_name)`` for ``industry_id``.

    ``(None, None)`` when the IndustryId is not in the industries.csv lookup.
    """
    df = _load_industries()
    hit = df[df["IndustryId"] == industry_id]
    if hit.empty:
        return (None, None)
    row = hit.iloc[0]
    return (str(row["Industry"]), str(row["Sector"]))


__all__ = ["SIMFIN_CACHE_DIR", "get_industry_id", "industry_label", "iter_industry_peers"]

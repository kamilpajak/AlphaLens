"""SMD cache + PIT universe primitives for retrospective audits.

Hosts the universe-construction and cache-management functions that previously
lived inside ``paper_trade.scorer_v9d``. Extracted on 2026-05-25 when the
paper-trade Layer 5 infrastructure was retired (iVolatility subscription
ended 2026-05-10; v9D forward observation no longer producible). These
primitives stay because retrospective scripts under ``scripts/`` still
consume them for U1/U2/U3 universe rebuilds.

Public surface:
- ``DEFAULT_PIT_DIR`` / ``DEFAULT_SMD_CACHE_DIR`` / ``DEFAULT_ETFS`` — paths + seed
- ``pit_union`` — union of yaml PIT snapshots from ``start_year`` onward
- ``backfill_smd_history`` — prepend earlier rows to per-ticker SMD parquets
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yaml

logger = logging.getLogger(__name__)

DEFAULT_PIT_DIR = Path.home() / ".alphalens" / "pit_universe"
DEFAULT_SMD_CACHE_DIR = Path.home() / ".alphalens" / "ivolatility_smd"
DEFAULT_ETFS: tuple[str, ...] = ("SPY", "QQQ", "IWM", "MDY", "EFA", "EEM", "TLT", "GLD")


def pit_union(
    *,
    start_year: int = 2018,
    pit_dir: Path | None = None,
    extra_etfs: tuple[str, ...] = DEFAULT_ETFS,
) -> list[str]:
    """Union of all PIT-universe yaml snapshots from ``start_year`` onward."""
    pit_dir = pit_dir or DEFAULT_PIT_DIR
    union: set[str] = set()
    for p in sorted(pit_dir.glob("*.yaml")):
        try:
            snap_year = int(p.stem.split("-")[0])
        except ValueError:
            continue
        if snap_year < start_year:
            continue
        data = yaml.safe_load(p.read_text()) or {}
        for t in data.get("tickers", []):
            union.add(str(t).upper())
    union |= set(extra_etfs)
    return sorted(union)


def backfill_smd_history(
    tickers: list[str],
    *,
    target_start: date,
    cache_dir: Path | None = None,
    fetcher: Callable[[str, date, date], pd.DataFrame | None] | None = None,
    sleep_between: float = 0.2,
) -> dict[str, int]:
    """Prepend pre-existing-min-date rows to per-ticker SMD parquets.

    Reads each ticker's existing parquet, finds the earliest ``tradeDate``,
    and fetches ``[target_start, earliest)`` if ``earliest > target_start``.
    Missing tickers (no parquet) are skipped — bulk seed must run first via
    ``download_and_cache``. Returns a counts dict:
    ``{"backfilled": N, "skipped_already_covered": N, "skipped_no_coverage": N,
    "skipped_missing": N, "errors": N}``.
    """
    cache_dir = cache_dir or DEFAULT_SMD_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)

    if fetcher is None:
        from alphalens_pipeline.data.alt_data.ivolatility_smd_cache import (
            _robust_smd_fetcher,
        )

        fetcher = _robust_smd_fetcher

    counts = {
        "backfilled": 0,
        "skipped_already_covered": 0,
        "skipped_no_coverage": 0,
        "skipped_missing": 0,
        "errors": 0,
    }

    for ticker in tickers:
        outcome = _backfill_one_ticker(ticker, cache_dir, fetcher, target_start)
        counts[outcome] = counts.get(outcome, 0) + 1
        if sleep_between > 0:
            import time

            time.sleep(sleep_between)

    return counts


def _backfill_one_ticker(
    ticker: str,
    cache_dir: Path,
    fetcher: Callable[[str, date, date], pd.DataFrame | None],
    target_start: date,
) -> str:
    """Backfill one ticker's parquet; returns the counts-dict key for outcome."""
    from alphalens_pipeline.data.alt_data.ivolatility_smd_cache import (
        _coerce_mixed_object_columns,
    )

    path = cache_dir / f"{ticker.upper()}.parquet"
    if not path.exists():
        return "skipped_missing"
    try:
        existing = pd.read_parquet(path)
    except Exception as exc:
        logger.warning("[%s] parquet read failed: %s", ticker, exc)
        return "errors"
    if existing.empty or "tradeDate" not in existing.columns:
        return "errors"
    min_date_ts = pd.to_datetime(existing["tradeDate"], errors="coerce").min()
    if pd.isna(min_date_ts):
        logger.warning("[%s] tradeDate column has no valid timestamps", ticker)
        return "errors"
    min_date = min_date_ts.date()
    if min_date <= target_start:
        return "skipped_already_covered"
    try:
        new_df = fetcher(ticker, target_start, min_date - timedelta(days=1))
    except Exception as exc:
        logger.warning("[%s] backfill fetch failed: %s", ticker, exc)
        return "errors"
    if new_df is None or (isinstance(new_df, pd.DataFrame) and new_df.empty):
        return "skipped_no_coverage"
    try:
        new_df = _coerce_mixed_object_columns(new_df)
        combined = pd.concat([new_df, existing], ignore_index=True)
        combined = combined.drop_duplicates(subset=["tradeDate"], keep="last")
        combined = combined.sort_values("tradeDate").reset_index(drop=True)
        combined.to_parquet(path)
        return "backfilled"
    except Exception as exc:
        logger.warning("[%s] backfill write failed: %s", ticker, exc)
        return "errors"

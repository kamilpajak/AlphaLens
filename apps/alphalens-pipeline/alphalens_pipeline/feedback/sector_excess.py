"""Sector-relative EDGE outcome (PR-2b, D4 decoupling).

``sector_excess_return = forward_return − sector_etf_window_return`` measures a
candidate's forward return against ITS OWN SPDR sector ETF over the SAME
arrival→exit window as the SPY benchmark-excess. The outcome benchmark is now a
different series from the SPY-derived market_state label, breaking the
SPY-on-SPY confound so the deferred H-B study is not a pure SPY-autocorrelation
artifact (memo §4.2, D4 resolution).

Reuses the exact window machinery of ``benchmark_excess`` (arrival opening-window
VWAP → last-bar close, same 480-min horizon) by delegating to
``compute_market_excess_for_row`` with the candidate's sector ETF as the
benchmark ticker — so the sector metric shares the SPY metric's window
convention verbatim. Only the per-row benchmark TICKER differs (resolved via
``sector_etf``). A row whose sector is unresolvable is EXCLUDED (all-None), never
benchmarked against SPY. UNVALIDATED forward-log; poolability key
``OUTCOME_BENCHMARK_VERSION`` (encodes the SIC→ETF map version).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path
from typing import Any

import pandas as pd

from alphalens_pipeline.data.fundamentals.sector_etf import (
    SECTOR_ETF_MAP_VERSION,
    sector_etf_for_ticker,
)
from alphalens_pipeline.feedback.benchmark_excess import (
    BarFetch,
    compute_market_excess_for_row,
)
from alphalens_pipeline.paper.calendar import DEFAULT_EXCHANGE, previous_trading_day

logger = logging.getLogger(__name__)

# Poolability key for the sector-relative outcome: the metric family + the
# SIC→ETF map version, so the deferred H-B study never pools rows across map
# revisions (a re-mapped sector is a different benchmark).
OUTCOME_BENCHMARK_VERSION = f"sector-etf-v1-{SECTOR_ETF_MAP_VERSION}"

# The four columns this module writes onto every store parquet.
SECTOR_EXCESS_COLUMNS = (
    "sector_etf_ticker",
    "sector_etf_window_return",
    "sector_excess_return",
    "outcome_benchmark_version",
)


def _default_bar_fetch(ticker: str, start: dt.datetime, end: dt.datetime) -> list[dict[str, Any]]:
    """Production bar source: the canonical Polygon client minute aggregates."""
    from alphalens_pipeline.data.alt_data.polygon_client import get_default_polygon_client

    return list(get_default_polygon_client().get_agg_range(ticker=ticker, start=start, end=end))


def compute_sector_excess_for_row(
    row: dict[str, Any],
    *,
    bar_fetch: BarFetch,
    last_closed_session: dt.date,
    exchange: str = DEFAULT_EXCHANGE,
) -> tuple[str | None, float | None, float | None]:
    """``(sector_etf_ticker, sector_etf_window_return, sector_excess_return)``.

    A row whose sector is unresolvable returns ``(None, None, None)`` — EXCLUDED,
    never benchmarked against SPY (memo §4.2). A resolved sector whose window is
    unrecoverable (no ``forward_return`` / degenerate window / empty fetch) keeps
    the ETF ticker but null metrics — never a fudged value.
    """
    ticker = row.get("ticker")
    etf = sector_etf_for_ticker(str(ticker)) if ticker else None
    if etf is None:
        return None, None, None
    window_return, excess = compute_market_excess_for_row(
        row,
        bar_fetch=bar_fetch,
        last_closed_session=last_closed_session,
        benchmark_ticker=etf,
        exchange=exchange,
    )
    return etf, window_return, excess


def _memoizing_fetch(fetch: BarFetch) -> BarFetch:
    """Wrap ``fetch`` so identical ``(ticker, start, end)`` windows fetch once per
    run. Many candidates share the same sector ETF + arrival/exit window, so one
    Polygon call serves them all — the benchmark_excess per-window cache
    generalised to a per-ticker key so distinct sector ETFs never collide."""
    cache: dict[tuple[str, dt.datetime, dt.datetime], list[dict[str, Any]]] = {}

    def wrapped(ticker: str, start: dt.datetime, end: dt.datetime) -> list[dict[str, Any]]:
        key = (ticker, start, end)
        if key not in cache:
            cache[key] = list(fetch(ticker, start, end))
        return cache[key]

    return wrapped


def _write_atomic(path: Path, df: pd.DataFrame) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp)
    os.replace(tmp, path)


def enrich_store_with_sector_excess(
    store_dir: Path | str,
    *,
    bar_fetch: BarFetch | None = None,
    now: dt.datetime | None = None,
    exchange: str = DEFAULT_EXCHANGE,
    deadline: Any = None,
) -> int:
    """Add / refresh the sector-excess columns on every store parquet.

    Mirrors ``enrich_store_with_benchmark_excess`` but resolves a PER-ROW sector
    ETF benchmark. Returns the number of rows that got a non-null
    ``sector_excess_return``. Deadline handling matches benchmark-excess: a trip
    mid-file leaves that parquet untouched so unprocessed rows retry next run.
    """
    store = Path(store_dir)
    if not store.exists():
        return 0
    fetch = _memoizing_fetch(bar_fetch or _default_bar_fetch)
    now = now or dt.datetime.now(dt.UTC)
    last_closed_session = previous_trading_day(now.date(), exchange)
    n_enriched = 0

    for path in sorted(store.glob("*.parquet")):
        try:
            df = pd.read_parquet(path)
        except (OSError, ValueError) as exc:
            logger.warning("sector-excess: bad store parquet %s — %s; skipping.", path, exc)
            continue

        etf_col: list[str | None] = []
        wret_col: list[float | None] = []
        excess_col: list[float | None] = []
        stopped_early = False
        for _, row in df.iterrows():
            if deadline is not None and deadline.should_stop():
                stopped_early = True
                break
            etf, wret, excess = compute_sector_excess_for_row(
                dict(row),
                bar_fetch=fetch,
                last_closed_session=last_closed_session,
                exchange=exchange,
            )
            etf_col.append(etf)
            wret_col.append(wret)
            excess_col.append(excess)
            if excess is not None:
                n_enriched += 1

        if stopped_early:
            break

        df["sector_etf_ticker"] = etf_col
        df["sector_etf_window_return"] = wret_col
        df["sector_excess_return"] = excess_col
        df["outcome_benchmark_version"] = OUTCOME_BENCHMARK_VERSION
        _write_atomic(path, df)

    return n_enriched


__all__ = [
    "OUTCOME_BENCHMARK_VERSION",
    "SECTOR_EXCESS_COLUMNS",
    "compute_sector_excess_for_row",
    "enrich_store_with_sector_excess",
]

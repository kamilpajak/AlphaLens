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

Discipline (#1435), the same the SPY pass has:

* **Terminal rows only.** The pair is computed for rows whose ``matured_at`` is
  set — the window is fixed there. An ongoing row gets its ``sector_etf_ticker``
  (resolution is a free in-process lookup) and no pair: nothing reads a sector
  leg before maturity (the pre-registered H-B estimand is a matured outcome),
  and the moving-window recompute was ~107 fetches a night. A
  ``SPLIT_INVALIDATED`` quarantine is treated the same way (#1452,
  ``benchmark_excess.row_is_quarantined``): ETF label, no pair, no fetch, and a
  pair it once carried is nulled rather than reused — its ``forward_return`` is
  raw replay telemetry, not an outcome.
* **Reuse-first.** A terminal row whose stored pair is real, arithmetically
  consistent with ``forward_return``, recorded over THIS ``matured_at``
  (``sector_window_exit``), computed against the ETF the ticker resolves to
  TODAY, and stamped with the current map version is settled — carried with no
  fetch. The ETF equality matters because ``sic_index.parquet`` is refreshed by
  hand with ``SECTOR_ETF_MAP_VERSION`` unchanged, so a ticker can change sector
  under the same version; without it a reused pair could sit under the wrong
  label — and the wrong benchmark series — for ever, and the poolability key
  would pool two series.
* **Newest-first; a deadline trip mid-file leaves the file untouched and its
  rows uncounted; a file is written only when something changed** (an absent
  column counts as changed), so the hourly Django mirror is not woken by no-op
  rewrites.
* **Per-run float cache** keyed ``(etf, ladder arrival, exit)`` — seeded from
  reused rows and consulted before any fetch — instead of a raw-bar memo. The
  arrival is ``ladder_arrival_session(brief_date)`` for EVERY row, event lane
  included, because that is what ``compute_market_excess_for_row`` fetches with;
  the event-CAR anchor differs on most dates and must never key this cache.
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
    _as_date,
    _is_real,
    compute_market_excess_for_row,
    row_is_quarantined,
    stored_pair_is_settled,
)
from alphalens_pipeline.feedback.ladder_config import ladder_arrival_session
from alphalens_pipeline.paper.calendar import DEFAULT_EXCHANGE, previous_trading_day

logger = logging.getLogger(__name__)

# Poolability key for the sector-relative outcome: the metric family + the
# SIC→ETF map version, so the deferred H-B study never pools rows across map
# revisions (a re-mapped sector is a different benchmark).
OUTCOME_BENCHMARK_VERSION = f"sector-etf-v1-{SECTOR_ETF_MAP_VERSION}"

# The five columns this module writes onto every store parquet.
# ``sector_window_exit`` is parquet-only (no Django field; the ingest copies model
# fields only, the same precedent as ``benchmark_window_exit``).
SECTOR_EXCESS_COLUMNS = (
    "sector_etf_ticker",
    "sector_etf_window_return",
    "sector_excess_return",
    "sector_window_exit",
    "outcome_benchmark_version",
)

# (etf, ladder arrival session, exit session) -> the ETF's window return, or
# ``None`` when the window could not be computed this run (not retried within
# the run, same policy as the SPY pass's window cache).
WindowReturns = dict[tuple[str, dt.date, dt.date], float | None]


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

    The unguarded per-row primitive: no terminal-only gate, no quarantine gate,
    no reuse-first, no cache. ``enrich_store_with_sector_excess`` is the only supported entry point
    for a store; this exists for the row-level tests of the resolution rules.

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


def _write_atomic(path: Path, df: pd.DataFrame) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp)
    os.replace(tmp, path)


def _row_is_settled(row: pd.Series, etf: str) -> bool:
    """The reuse-first gate: the shared pair predicate plus the two facts only
    this pass knows — the ETF the ticker resolves to today and the map version."""
    if str(row.get("sector_etf_ticker") or "") != etf:
        return False
    if row.get("outcome_benchmark_version") != OUTCOME_BENCHMARK_VERSION:
        return False
    return stored_pair_is_settled(
        row,
        window_col="sector_etf_window_return",
        excess_col="sector_excess_return",
        exit_col="sector_window_exit",
    )


def _cell_equal(a: Any, b: Any) -> bool:
    if not _is_real(a) and not _is_real(b):
        return True
    if not _is_real(a) or not _is_real(b):
        return False
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-12
        except (TypeError, ValueError):
            return False
    return a == b


def _column_changed(df: pd.DataFrame, name: str, values: list[Any]) -> bool:
    if name not in df.columns:
        return True
    current = df[name].tolist()
    return any(not _cell_equal(c, n) for c, n in zip(current, values, strict=True))


def _enrich_one_file(
    path: Path,
    *,
    fetch: BarFetch,
    last_closed_session: dt.date,
    exchange: str,
    deadline: Any,
    window_returns: WindowReturns,
) -> tuple[int, int, int, bool]:
    """Enrich one store parquet in place.

    Returns ``(n_enriched, n_reused, n_fetched, stopped_early)``. A read error
    skips the file. A deadline trip mid-file leaves the parquet UNTOUCHED and
    reports ``stopped_early=True``; the caller must not count its rows. The file
    is rewritten only when a column would actually change.
    """
    try:
        df = pd.read_parquet(path)
    except (OSError, ValueError) as exc:
        logger.warning("sector-excess: bad store parquet %s — %s; skipping.", path, exc)
        return 0, 0, 0, False

    etf_col: list[str | None] = []
    wret_col: list[float | None] = []
    excess_col: list[float | None] = []
    exit_col: list[str | None] = []
    n_enriched = n_reused = n_fetched = 0
    for _, row in df.iterrows():
        if deadline is not None and deadline.should_stop():
            return 0, 0, 0, True
        ticker = row.get("ticker")
        etf = sector_etf_for_ticker(str(ticker)) if ticker else None
        matured = _as_date(row.get("matured_at"))
        if etf is None or matured is None or row_is_quarantined(row):
            # Unresolvable sector, an ongoing row, or a SPLIT_INVALIDATED
            # quarantine (#1452): ETF resolved, no pair, no fetch. The
            # quarantine check sits before the reuse gate on purpose — a stored
            # pair can be settled and would otherwise be carried for ever.
            etf_col.append(etf)
            wret_col.append(None)
            excess_col.append(None)
            exit_col.append(None)
            continue
        brief_date = _as_date(row.get("brief_date"))
        arrival = ladder_arrival_session(brief_date, exchange) if brief_date is not None else None
        key = (etf, arrival, matured) if arrival is not None else None
        if _row_is_settled(row, etf):
            wret = float(row["sector_etf_window_return"])
            excess = float(row["sector_excess_return"])
            n_reused += 1
            n_enriched += 1
            if key is not None:
                window_returns.setdefault(key, wret)
        else:
            forward = row.get("forward_return")
            n_fetched += 1
            if not _is_real(forward):
                # No candidate leg: nothing to compute and NOTHING to cache — a
                # None written here would deny every sibling of this window a
                # fetch for the rest of the run (review finding on #1435).
                wret, excess = None, None
            elif key is not None and key in window_returns:
                wret = window_returns[key]
                excess = float(forward) - wret if wret is not None else None
            else:
                wret, excess = compute_market_excess_for_row(
                    dict(row),
                    bar_fetch=fetch,
                    last_closed_session=last_closed_session,
                    benchmark_ticker=etf,
                    exchange=exchange,
                )
                if key is not None:
                    # A failed fetch caches None on purpose: one attempt per
                    # window per run, the SPY pass's policy; it is retried next run.
                    window_returns[key] = wret
            if excess is not None:
                n_enriched += 1
        etf_col.append(etf)
        wret_col.append(wret)
        excess_col.append(excess)
        exit_col.append(matured.isoformat() if wret is not None else None)

    new_columns: dict[str, list[Any]] = {
        "sector_etf_ticker": etf_col,
        "sector_etf_window_return": wret_col,
        "sector_excess_return": excess_col,
        "sector_window_exit": exit_col,
        "outcome_benchmark_version": [OUTCOME_BENCHMARK_VERSION] * len(df),
    }
    if any(_column_changed(df, name, values) for name, values in new_columns.items()):
        for name, values in new_columns.items():
            df[name] = values
        _write_atomic(path, df)
    return n_enriched, n_reused, n_fetched, False


def enrich_store_with_sector_excess(
    store_dir: Path | str,
    *,
    bar_fetch: BarFetch | None = None,
    now: dt.datetime | None = None,
    exchange: str = DEFAULT_EXCHANGE,
    deadline: Any = None,
) -> int:
    """Add / refresh the sector-excess columns on every store parquet.

    Mirrors ``enrich_store_with_benchmark_excess``: newest-first, reuse-first,
    a deadline trip mid-file leaves that parquet untouched and stops the sweep
    (the deadline latches, so later files would only be opened to skip). Returns
    the number of PERSISTED rows carrying a non-null ``sector_excess_return``.
    """
    store = Path(store_dir)
    if not store.exists():
        logger.info("sector-excess: enriched 0 (reused 0, fetched 0, stopped_early=False)")
        return 0
    fetch = bar_fetch or _default_bar_fetch
    now = now or dt.datetime.now(dt.UTC)
    last_closed_session = previous_trading_day(now.date(), exchange)
    window_returns: WindowReturns = {}
    n_enriched = n_reused = n_fetched = 0
    stopped_early = False

    for path in sorted(store.glob("*.parquet"), reverse=True):
        delta, reused, fetched, stopped = _enrich_one_file(
            path,
            fetch=fetch,
            last_closed_session=last_closed_session,
            exchange=exchange,
            deadline=deadline,
            window_returns=window_returns,
        )
        if stopped:
            # Nothing from this file was persisted, so nothing from it is counted.
            stopped_early = True
            break
        n_enriched += delta
        n_reused += reused
        n_fetched += fetched

    logger.info(
        "sector-excess: enriched %d (reused %d, fetched %d, stopped_early=%s)",
        n_enriched,
        n_reused,
        n_fetched,
        stopped_early,
    )
    return n_enriched


__all__ = [
    "OUTCOME_BENCHMARK_VERSION",
    "SECTOR_EXCESS_COLUMNS",
    "compute_sector_excess_for_row",
    "enrich_store_with_sector_excess",
]

"""Event-anchored outcome for the event lane (epic #1293, #1297).

``car_20_event`` / ``car_40_event`` = the pre-registered primary / secondary
outcome of the insider-cluster lane (``docs/research/insider_cluster_forward_prereg_2026_09.md``
section 6): the stock's OPEN(arrival) -> CLOSE(arrival + 19 / + 39 sessions)
buy-and-hold return minus SPY's over the same window, beta = 1, with the split
guard (every consecutive-close ratio inside [0.55, 1.8], else null). By the
lane's brief-date rule the population monitor's ``session_on_or_after(brief_date)``
IS the event arrival, so the anchor is derived from ``brief_date`` alone.

Computed disk-first from the monitor's grouped-daily cache (raw ``adjusted=false``
whole-market daily bars, one file per session, SPY included); a session the
cache lacks is fetched once through the monitor's own prefetch. Stamped only on
EVENT rows (``source == "insider_cluster"`` or ``event_overlap``) — thematic rows
keep the columns null; the two lanes are never pooled. Parquet-only by design:
the pre-registered look reads the store, nothing on the wire shows the value
before the floor (no interim peeks).

Shape mirrors ``sector_excess``: newest-first over every store parquet, atomic
rewrite only when something changed, a deadline trip leaves the file untouched.
"""

from __future__ import annotations

import datetime as dt
import itertools
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from alphalens_pipeline.events.insider_cluster import (
    HORIZON_SESSIONS_PRIMARY,
    HORIZON_SESSIONS_SECONDARY,
    SOURCE_INSIDER_CLUSTER,
    SPLIT_RATIO_HI,
    SPLIT_RATIO_LO,
)
from alphalens_pipeline.feedback.benchmark_excess import DEFAULT_BENCHMARK_TICKER
from alphalens_pipeline.feedback.population_ladder_monitor import (
    GroupedFetch,
    _default_grouped_fetch,
    _prefetch_grouped_daily,
)
from alphalens_pipeline.paper.calendar import (
    DEFAULT_EXCHANGE,
    advance_trading_sessions,
    previous_trading_day,
    session_on_or_after,
)

logger = logging.getLogger(__name__)

# Poolability key: bump on any change to the anchor / horizon / guard / benchmark.
EVENT_CAR_VERSION = "event-car-v1"
EVENT_CAR_COLUMNS = ("car_20_event", "car_40_event", "event_car_version")
BENCHMARK_TICKER = DEFAULT_BENCHMARK_TICKER

GroupedBySession = Mapping[dt.date, Mapping[str, Mapping[str, Any]] | None]


def is_event_row(row: Mapping[Any, Any]) -> bool:
    """An insider-cluster row or a thematic card that also carried a cluster."""
    if row.get("source") == SOURCE_INSIDER_CLUSTER:
        return True
    overlap = row.get("event_overlap")
    return isinstance(overlap, (bool, np.bool_)) and bool(overlap)


def _price(grouped: GroupedBySession, session: dt.date, ticker: str, field: str) -> float | None:
    day = grouped.get(session)
    if not day:
        return None
    bar = day.get(ticker.upper())
    if not bar:
        return None
    raw = bar.get(field)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(value) or value <= 0:
        return None
    return value


def event_car_from_grouped(
    grouped_by_session: GroupedBySession,
    ticker: str,
    *,
    arrival: dt.date,
    horizon_sessions: int,
    benchmark_ticker: str = BENCHMARK_TICKER,
    exchange: str = DEFAULT_EXCHANGE,
) -> float | None:
    """OPEN(arrival) -> CLOSE(arrival + horizon) minus the benchmark's, or ``None``.

    ``None`` when any price in the window is missing / non-positive (the guard
    needs EVERY close in the window) or the split guard trips.
    """
    sessions = [advance_trading_sessions(arrival, i, exchange) for i in range(horizon_sessions + 1)]
    closes = [_price(grouped_by_session, s, ticker, "c") for s in sessions]
    if any(c is None for c in closes):
        return None
    open_a = _price(grouped_by_session, arrival, ticker, "o")
    bench_o = _price(grouped_by_session, arrival, benchmark_ticker, "o")
    bench_c = _price(grouped_by_session, sessions[-1], benchmark_ticker, "c")
    if open_a is None or bench_o is None or bench_c is None:
        return None
    series = [float(c) for c in closes if c is not None]
    for prev, cur in itertools.pairwise(series):
        ratio = cur / prev
        if ratio < SPLIT_RATIO_LO or ratio > SPLIT_RATIO_HI:
            return None
    return (series[-1] / open_a - 1.0) - (bench_c / bench_o - 1.0)


def _coerce_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and value:
        return dt.date.fromisoformat(value[:10])
    if isinstance(value, pd.Timestamp):
        return value.date()
    return None


def compute_event_car_for_row(
    row: Mapping[Any, Any],
    *,
    grouped_by_session: GroupedBySession,
    last_closed_session: dt.date,
    exchange: str = DEFAULT_EXCHANGE,
) -> tuple[float | None, float | None]:
    """``(car_20_event, car_40_event)``; a horizon not yet closed is ``None`` (not matured)."""
    brief_date = _coerce_date(row.get("brief_date"))
    ticker = row.get("ticker")
    if brief_date is None or not ticker:
        return None, None
    arrival = session_on_or_after(brief_date, exchange)
    out: list[float | None] = []
    for horizon in (HORIZON_SESSIONS_PRIMARY, HORIZON_SESSIONS_SECONDARY):
        if advance_trading_sessions(arrival, horizon, exchange) > last_closed_session:
            out.append(None)
            continue
        out.append(
            event_car_from_grouped(
                grouped_by_session,
                str(ticker),
                arrival=arrival,
                horizon_sessions=horizon,
                exchange=exchange,
            )
        )
    return out[0], out[1]


def _sessions_needed(
    brief_date: dt.date, last_closed_session: dt.date, exchange: str
) -> list[dt.date]:
    arrival = session_on_or_after(brief_date, exchange)
    out: list[dt.date] = []
    for i in range(HORIZON_SESSIONS_SECONDARY + 1):
        s = advance_trading_sessions(arrival, i, exchange)
        if s > last_closed_session:
            break
        out.append(s)
    return out


def _is_real(value: Any) -> bool:
    try:
        return value is not None and np.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _write_atomic(path: Path, df: pd.DataFrame) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp)
    os.replace(tmp, path)


def _enrich_one_file(
    path: Path,
    *,
    store: Path,
    grouped_fetch: GroupedFetch,
    last_closed_session: dt.date,
    exchange: str,
) -> int:
    try:
        df = pd.read_parquet(path)
    except (OSError, ValueError) as exc:
        logger.warning("event-car: bad store parquet %s — %s; skipping.", path, exc)
        return 0
    changed = False
    for col in EVENT_CAR_COLUMNS:
        if col not in df.columns:
            df[col] = None
            changed = True
    if "brief_date" not in df.columns or "ticker" not in df.columns:
        if changed:
            _write_atomic(path, df)
        return 0

    records = df.to_dict("records")
    todo = [i for i, r in enumerate(records) if is_event_row(r) and not _is_real(r["car_40_event"])]
    n_real = sum(1 for r in records if is_event_row(r) and _is_real(r["car_20_event"]))
    if not todo:
        if changed:
            _write_atomic(path, df)
        return n_real

    sessions: set[dt.date] = set()
    for i in todo:
        bd = _coerce_date(records[i].get("brief_date"))
        if bd is not None:
            sessions.update(_sessions_needed(bd, last_closed_session, exchange))
    grouped = _prefetch_grouped_daily(store, sorted(sessions), grouped_fetch, exchange)

    for i in todo:
        c20, c40 = compute_event_car_for_row(
            records[i],
            grouped_by_session=grouped,
            last_closed_session=last_closed_session,
            exchange=exchange,
        )
        for col, value in (("car_20_event", c20), ("car_40_event", c40)):
            old = records[i].get(col)
            old_real = _is_real(old)
            if (value is None) == old_real or (
                value is not None and old_real and abs(float(old) - value) > 1e-12  # type: ignore[arg-type]
            ):
                df.at[df.index[i], col] = value
                changed = True
        if records[i].get("event_car_version") != EVENT_CAR_VERSION:
            df.at[df.index[i], "event_car_version"] = EVENT_CAR_VERSION
            changed = True
        if c20 is not None and not _is_real(records[i].get("car_20_event")):
            n_real += 1

    if changed:
        _write_atomic(path, df)
    return n_real


def enrich_store_with_event_car(
    store_dir: Path | str,
    *,
    grouped_fetch: GroupedFetch | None = None,
    now: dt.datetime | None = None,
    exchange: str = DEFAULT_EXCHANGE,
    deadline: Any = None,
) -> int:
    """Stamp ``car_20_event`` / ``car_40_event`` on every event row of every store parquet.

    Returns the number of event rows carrying a real ``car_20_event`` after the
    pass. Disk-first (the grouped-daily cache); a session the cache lacks costs
    one grouped-daily call. A deadline trip stops BEFORE the next file so the
    file being written is never half-done. Never raises for one bad file.
    """
    store = Path(store_dir)
    if not store.exists():
        return 0
    now = now or dt.datetime.now(dt.UTC)
    last_closed_session = previous_trading_day(now.date(), exchange)
    fetch = grouped_fetch or _default_grouped_fetch
    n_real = 0
    for path in sorted(store.glob("*.parquet"), reverse=True):
        if deadline is not None and deadline.should_stop():
            break
        try:
            n_real += _enrich_one_file(
                path,
                store=store,
                grouped_fetch=fetch,
                last_closed_session=last_closed_session,
                exchange=exchange,
            )
        except Exception:  # one bad file must not stop the pass
            logger.exception("event-car: failed on %s; continuing", path)
    return n_real


__all__ = [
    "EVENT_CAR_COLUMNS",
    "EVENT_CAR_VERSION",
    "compute_event_car_for_row",
    "enrich_store_with_event_car",
    "event_car_from_grouped",
    "is_event_row",
]

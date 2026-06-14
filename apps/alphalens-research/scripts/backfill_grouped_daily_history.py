"""Build + maintain the split-adjusted grouped-daily history for the O'Neil RS term.

Two modes over one store (``~/.alphalens/grouped_daily_history/``, adjusted=True):

  ONE-TIME backfill (``--sessions 440``): fetch the ~440 trailing sessions (≈21 months,
    the full reachable Polygon free-tier window) newest→oldest, so a 252-session RS
    lookback is on disk for ANY recent asof at launch. ~88 min wall @ 5 req/min — RUN IN A
    QUIET OFF-MARKET WINDOW so it does not 429-starve the live monitor + shadow-returns
    jobs (shared per-key free quota). On NOT_AUTHORIZED (past-entitlement cliff) it STOPS
    descending — the free tier serves only ~21–24 months, a moving window.

  TOP-UP (``--topup``): self-sizing — fetch every not-on-disk session from the store's
    newest date forward to yesterday (capped ``--max-catchup-days``), so a multi-day VPS
    outage auto-heals on the next run instead of leaving a permanent interior gap that nulls
    RS for ~252 asofs. Steady-state = ONE Polygon call/session.

Idempotent/resumable: any date already on disk is skipped (never re-fetched). A fetch error
or empty payload on a confirmed trading day is a GAP — NOT written (so the date retries next
run), never an empty parquet (which would freeze a phantom empty market). Routes every fetch
through ``rs_history`` (the canonical PolygonClient) — no polygon.io URL literal here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from alphalens_pipeline.data import rs_history
from alphalens_pipeline.data.alt_data.polygon_client import PolygonAuthError
from alphalens_pipeline.paper.calendar import DEFAULT_EXCHANGE, is_trading_day, n_sessions_before

logger = logging.getLogger("backfill_grouped_daily_history")

_DEFAULT_SESSIONS = 440  # ≈21 months — the full reachable free-tier window
_DEFAULT_MAX_CATCHUP_DAYS = 400


@dataclass
class BackfillResult:
    fetched: int = 0
    skipped_existing: int = 0
    gaps: int = 0
    stopped_on_entitlement: bool = False


def _is_entitlement_error(exc: Exception) -> bool:
    """A past-history-entitlement cliff (free-tier depth exceeded), not a transient error."""
    if isinstance(exc, PolygonAuthError):
        return True
    msg = str(exc).upper()
    return "NOT_AUTHORIZED" in msg or "ENTITLE" in msg


def _trading_sessions_in_range(start: dt.date, end: dt.date, exchange: str) -> list[dt.date]:
    """Every trading session in ``[start, end]`` inclusive, ascending."""
    out: list[dt.date] = []
    d = start
    while d <= end:
        if is_trading_day(d, exchange):
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def one_time_sessions(today: dt.date, sessions: int, exchange: str) -> list[dt.date]:
    """The ``sessions`` trailing trading days ending at the session on-or-before yesterday."""
    end = n_sessions_before(today - dt.timedelta(days=1), 0, exchange)  # last session <= yesterday
    start = n_sessions_before(end, sessions - 1, exchange)
    return _trading_sessions_in_range(start, end, exchange)


def topup_sessions(
    root: Path, today: dt.date, max_catchup_days: int, exchange: str
) -> list[dt.date]:
    """Self-sizing: not-on-disk trading sessions from the store's newest date forward to
    yesterday, floored at ``today - max_catchup_days`` (cold-start / long-outage cap)."""
    end = n_sessions_before(today - dt.timedelta(days=1), 0, exchange)
    floor = today - dt.timedelta(days=max_catchup_days)
    newest = _newest_on_disk(root)
    start = max(newest + dt.timedelta(days=1), floor) if newest is not None else floor
    return _trading_sessions_in_range(start, end, exchange)


def _newest_on_disk(root: Path) -> dt.date | None:
    if not root.exists():
        return None
    dates: list[dt.date] = []
    for p in root.glob("*.parquet"):
        try:
            dates.append(dt.date.fromisoformat(p.stem))
        except ValueError:
            continue
    return max(dates) if dates else None


def run_backfill(
    root: Path,
    sessions_to_fetch: list[dt.date],
    *,
    grouped_fetch: rs_history.GroupedFetch,
    stop_on_entitlement: bool,
) -> BackfillResult:
    """Fetch each session not already on disk; skip existing; gaps don't write.

    ``sessions_to_fetch`` is consumed in the given order. For the one-time backfill pass it
    is NEWEST→OLDEST so ``stop_on_entitlement`` halts cleanly at the free-tier history cliff.
    """
    result = BackfillResult()
    for date in sessions_to_fetch:
        if rs_history.read_grouped_day(root, date) is not None:
            result.skipped_existing += 1
            continue
        try:
            payload = grouped_fetch(date)
        except Exception as exc:
            if stop_on_entitlement and _is_entitlement_error(exc):
                logger.warning("entitlement cliff at %s (%s); stopping descent.", date, exc)
                result.stopped_on_entitlement = True
                break
            logger.warning("fetch failed for %s — %s; leaving as a gap to retry.", date, exc)
            result.gaps += 1
            continue
        if not payload:  # empty session payload on a confirmed trading day == gap, do NOT write
            logger.info("empty payload for %s; leaving as a gap to retry.", date)
            result.gaps += 1
            continue
        rs_history.write_grouped_day_atomic(root, date, payload)
        result.fetched += 1
        logger.info("fetched %s (%d tickers)", date, len(payload))
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0])
    p.add_argument("--root", type=Path, default=rs_history.DEFAULT_RS_HISTORY_ROOT)
    p.add_argument(
        "--sessions", type=int, default=_DEFAULT_SESSIONS, help="one-time backfill depth"
    )
    p.add_argument("--topup", action="store_true", help="self-sizing daily top-up mode")
    p.add_argument("--max-catchup-days", type=int, default=_DEFAULT_MAX_CATCHUP_DAYS)
    p.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    today = dt.datetime.now(dt.UTC).date()

    if args.topup:
        targets = topup_sessions(args.root, today, args.max_catchup_days, args.exchange)
        # Oldest→newest for top-up; no entitlement-cliff stop (only recent dates).
        result = run_backfill(
            args.root,
            targets,
            grouped_fetch=rs_history._default_grouped_fetch,
            stop_on_entitlement=False,
        )
    else:
        targets = one_time_sessions(today, args.sessions, args.exchange)
        # Newest→oldest so the cliff halts the descent cleanly.
        result = run_backfill(
            args.root,
            list(reversed(targets)),
            grouped_fetch=rs_history._default_grouped_fetch,
            stop_on_entitlement=True,
        )

    logger.info(
        "done: fetched=%d skipped_existing=%d gaps=%d cliff=%s",
        result.fetched,
        result.skipped_existing,
        result.gaps,
        result.stopped_on_entitlement,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

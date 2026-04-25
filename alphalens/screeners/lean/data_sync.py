"""Daily sync: pull Polygon grouped-daily → filter universe → write Lean CSV.

Tracks the last-synced trading date in a small state file so incremental runs
are cheap. Bootstrap (first-ever run) iterates backward from today for
`LEAN_DEFAULTS["history_bootstrap_days"]` days at 5 calls/min.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .lean_csv_writer import DailyBar, LeanCsvWriter
from .polygon_client import PolygonClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncReport:
    dates_synced: list[str]
    tickers_written: int
    bars_written: int


def _ts_to_yyyymmdd(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y%m%d")


class PolygonLeanSync:
    """Polygon grouped-daily → universe filter → Lean CSV merge."""

    def __init__(
        self,
        client: PolygonClient,
        writer: LeanCsvWriter,
        universe: Iterable[str],
        state_path: Path,
    ):
        self.client = client
        self.writer = writer
        self.universe: frozenset[str] = frozenset(t.upper() for t in universe)
        self.state_path = Path(state_path)

    # ---- state -----------------------------------------------------------------

    def load_last_synced(self) -> date | None:
        if not self.state_path.exists():
            return None
        try:
            data = json.loads(self.state_path.read_text())
            return date.fromisoformat(data["last_synced"])
        except (ValueError, KeyError, json.JSONDecodeError):
            return None

    def save_last_synced(self, d: date) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"last_synced": d.isoformat()}))

    # ---- sync ------------------------------------------------------------------

    def sync_date(self, day: date) -> tuple[int, int]:
        """Fetch one trading day, merge into each ticker's Lean CSV.

        Returns (tickers_written, bars_written).
        """
        iso = day.isoformat()
        logger.info("polygon grouped-daily fetch date=%s", iso)
        bars = self.client.grouped_daily(iso, adjusted=True)

        # Group bars by (ticker in universe).
        by_ticker: dict[str, list[DailyBar]] = {}
        for row in bars:
            if row.ticker not in self.universe:
                continue
            bar = DailyBar(
                date=_ts_to_yyyymmdd(row.timestamp_ms),
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
            )
            by_ticker.setdefault(row.ticker, []).append(bar)

        bars_written = 0
        for ticker, ticker_bars in by_ticker.items():
            self.writer.upsert_bars(ticker, ticker_bars)
            bars_written += len(ticker_bars)

        logger.info("synced date=%s tickers=%d bars=%d", iso, len(by_ticker), bars_written)
        return len(by_ticker), bars_written

    def sync_range(self, start: date, end: date) -> SyncReport:
        """Iterate `start..end` inclusive, skipping weekends (Polygon returns [] on closed days).

        Holidays are also handled (Polygon returns [] — we just move on).
        """
        if start > end:
            raise ValueError(f"start {start} after end {end}")

        dates_synced: list[str] = []
        total_tickers = 0
        total_bars = 0
        cursor = start
        while cursor <= end:
            if cursor.weekday() < 5:  # Mon-Fri
                t, b = self.sync_date(cursor)
                if t > 0:
                    dates_synced.append(cursor.isoformat())
                    total_tickers += t
                    total_bars += b
            cursor += timedelta(days=1)

        if dates_synced:
            self.save_last_synced(date.fromisoformat(dates_synced[-1]))

        return SyncReport(
            dates_synced=dates_synced,
            tickers_written=total_tickers,
            bars_written=total_bars,
        )

    def incremental_sync(self, today: date, bootstrap_days: int) -> SyncReport:
        """Sync from last_synced+1 up to `today-1`. Bootstrap if state is missing."""
        last = self.load_last_synced()
        if last is None:
            start = today - timedelta(days=bootstrap_days)
            logger.info("bootstrap sync: %s .. %s", start, today - timedelta(days=1))
        else:
            start = last + timedelta(days=1)
            logger.info("incremental sync: %s .. %s", start, today - timedelta(days=1))
        end = today - timedelta(days=1)
        if start > end:
            return SyncReport(dates_synced=[], tickers_written=0, bars_written=0)
        return self.sync_range(start, end)

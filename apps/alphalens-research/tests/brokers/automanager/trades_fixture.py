"""Shared fixture for the ``broker trades`` tests (#1701).

``fixtures/trades/live_2026_10_03/`` holds the LIVE keeper journals (current
files plus the compaction snapshots) as they stood on 2026-10-03, and the venue
rows read GET-only that day (audit order activities from 2026-08-01, the
trades and bookings reports, instrument details), with account identities
removed. Tests install the journals into an isolated home and answer the
fill-history capability from ``venue.json``. A test that needs a shape LIVE
never produced edits a copy and says so in its name (``synthetic_*``).
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from alphalens_pipeline.brokers.fill_history import (
    CostBooking,
    Execution,
    FillHistory,
    OrderActivity,
    ReadWindow,
)
from alphalens_pipeline.brokers.saxo.broker import SaxoBroker

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "trades" / "live_2026_10_03"
VENUE: dict[str, Any] = json.loads((FIXTURE_DIR / "venue.json").read_text(encoding="utf-8"))
NOW = dt.datetime(2026, 10, 3, 15, 30, tzinfo=dt.UTC)
JOURNALS = ("picks", "submissions", "entry_trails", "standalone_stops")


def install_journals(
    home: Path,
    env: str = "live",
    *,
    drop: Callable[[dict[str, Any]], bool] | None = None,
    extra: dict[str, list[dict[str, Any]]] | None = None,
    edit: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> Path:
    """Copy the LIVE journals into ``home``'s ``<env>`` state root.

    ``drop`` removes matching records from every file (current and snapshot);
    ``edit(journal, record)`` rewrites records; ``extra`` appends records to the
    CURRENT file of a journal. Returns the state root."""
    root = home / ".alphalens" / "broker_orders" / env
    (root / "compaction_snapshots").mkdir(parents=True, exist_ok=True)
    sources = [FIXTURE_DIR / f"{name}.jsonl" for name in JOURNALS] + sorted(
        (FIXTURE_DIR / "compaction_snapshots").glob("*.jsonl")
    )
    for source in sources:
        target = root / source.relative_to(FIXTURE_DIR)
        if drop is None and edit is None:
            shutil.copyfile(source, target)
            continue
        journal = source.name.split(".")[0]
        lines = []
        for raw in source.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            record = json.loads(raw)
            if drop is not None and drop(record):
                continue
            if edit is not None:
                record = edit(journal, record)
            lines.append(json.dumps(record, sort_keys=True))
        target.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    for journal, records in (extra or {}).items():
        with (root / f"{journal}.jsonl").open("a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
    return root


class FakeFillHistory:
    """Answers ``SupportsFillHistory`` from ``venue.json`` (or an edited copy)."""

    def __init__(
        self,
        venue: dict[str, Any] | None = None,
        *,
        sim: bool = False,
    ) -> None:
        self.venue = copy.deepcopy(VENUE if venue is None else venue)
        self.sim = sim
        self.calls: list[tuple[dt.datetime, dt.datetime]] = []
        self.tick_calls: list[int] = []

    def list_fill_history(self, since: dt.datetime, until: dt.datetime) -> FillHistory:
        self.calls.append((since, until))
        activities = tuple(
            activity
            for activity in (OrderActivity.from_vendor_row(r) for r in self.venue["audit"])
            if activity.activity_time is not None and since <= activity.activity_time <= until
        )
        audit_window = ReadWindow(since.isoformat(), until.isoformat(), len(activities))
        if self.sim:
            return FillHistory(
                activities=activities,
                executions=None,
                bookings=None,
                audit_window=audit_window,
                reports_skipped_reason="sim_reports_unusable",
            )
        executions = tuple(
            e
            for e in (Execution.from_vendor_row(r) for r in self.venue["trades"])
            if e.execution_time is not None and since <= e.execution_time <= until
        )
        kept = {e.trade_id for e in executions}
        bookings = tuple(
            b
            for b in (CostBooking.from_vendor_row(r) for r in self.venue["bookings"])
            if b.related_trade_id in kept
        )
        days = (
            (since.date() - dt.timedelta(days=1)).isoformat(),
            (until.date() + dt.timedelta(days=1)).isoformat(),
        )
        return FillHistory(
            activities=activities,
            executions=executions,
            bookings=bookings,
            audit_window=audit_window,
            trades_window=ReadWindow(*days, len(self.venue["trades"])),
            bookings_window=ReadWindow(*days, len(self.venue["bookings"])),
        )

    def tick_size(self, uic: int, price: float) -> float | None:
        self.tick_calls.append(uic)
        return SaxoBroker._tick_size_for(price, self.venue["instruments"][str(uic)])


def venue_without(
    *,
    orders: set[str] | None = None,
    rows: Callable[[dict[str, Any]], bool] | None = None,
    trades: set[str] | None = None,
) -> dict[str, Any]:
    """A copy of the venue rows with some removed."""
    venue = copy.deepcopy(VENUE)
    if orders:
        venue["audit"] = [r for r in venue["audit"] if r["OrderId"] not in orders]
    if rows is not None:
        venue["audit"] = [r for r in venue["audit"] if not rows(r)]
    if trades:
        venue["trades"] = [r for r in venue["trades"] if r["TradeId"] not in trades]
        venue["bookings"] = [r for r in venue["bookings"] if r["RelatedTradeId"] not in trades]
    return venue


def trade(report: Any, pick_key: str) -> dict[str, Any]:
    """The rendered record of ``pick_key`` from a :class:`TradesReport`."""
    for record in report.trades:
        if record["pick_key"] == pick_key:
            return record
    raise AssertionError(f"{pick_key} not in {[r['pick_key'] for r in report.trades]}")


def value(measured: dict[str, Any]) -> Any:
    return measured["value"]

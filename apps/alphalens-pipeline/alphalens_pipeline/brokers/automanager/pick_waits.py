"""Why an armed pick is waiting for free capital (#1734).

A pick that does not fit in free capital — the gross cap or the cash floor
says no — is not refused any more. It stays ``armed`` in ``picks.jsonl`` and
the drain tries it again every tick until it fits or its validity window ends
(``pick_window.py``). Nothing in ``picks.jsonl`` says that it is waiting, by
design: the queue's statuses are terminal facts, and a wait is not one.

This journal carries the wait instead, one JSON line under
``~/.alphalens/broker_orders/<env>/pick_waits.jsonl``:

``{pick_key, ticker, date, generation, ts, gate, message, window_end}``

The daemon appends a line when a pick starts waiting and again only when the
gate holding it changes, never once per tick. Two readers need it:

* `broker status` and `broker picks`, separate processes that must say why a
  pick is unplaced without sizing it (sizing is broker I/O);
* the daemon after a restart, which pages once per pick and must not page
  again for a wait it already announced.

A line is only a CLAIM about a pick that is still armed and unplaced. A reader
joins it to the queue and the submissions journal and ignores the line once
the pick was placed, disarmed or expired. Append-only like every journal here;
a malformed line is counted and skipped.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alphalens_pipeline.brokers.automanager import state_paths
from alphalens_pipeline.brokers.automanager.picks import STATUS_ARMED, PickRecord
from alphalens_pipeline.brokers.journal import append_json_line

GATE_GROSS_CAP = "gross_cap"
GATE_CASH_FLOOR = "cash_floor"
GATES: tuple[str, ...] = (GATE_GROSS_CAP, GATE_CASH_FLOOR)

_REQUIRED = ("pick_key", "gate")


@dataclass(frozen=True)
class PickWait:
    """The latest wait line of one pick, with the moment it started waiting."""

    pick_key: str
    gate: str
    message: str
    ts: str
    since: str
    window_end: str | None
    record: Mapping[str, Any]


@dataclass(frozen=True)
class PickWaitFold:
    latest: dict[str, PickWait]
    malformed: int


def append_wait(
    fields: Mapping[str, Any], *, now: dt.datetime | None = None, path: Path | None = None
) -> None:
    """Append one wait line stamped ``ts`` = ``now`` (the drain's clock; UTC now
    when omitted), so the line and the drain's own comparisons share one clock."""
    stamp = now if now is not None else dt.datetime.now(dt.UTC)
    record = {**fields, "ts": stamp.astimezone(dt.UTC).isoformat(timespec="seconds")}
    append_json_line(path or state_paths.pick_waits_path(), record, default=str)


def _parse(raw_line: str) -> dict[str, Any] | None:
    try:
        record = json.loads(raw_line)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    if any(not isinstance(record.get(key), str) or not record.get(key) for key in _REQUIRED):
        return None
    return record


def read_waits(*, path: Path | None = None) -> PickWaitFold:
    """The latest line per ``pick_key``; ``since`` is that pick's FIRST line."""
    target = path or state_paths.pick_waits_path()
    if not target.exists():
        return PickWaitFold(latest={}, malformed=0)
    latest: dict[str, PickWait] = {}
    first_ts: dict[str, str] = {}
    malformed = 0
    with target.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            if not raw_line.strip():
                continue
            record = _parse(raw_line)
            if record is None:
                malformed += 1
                continue
            key = record["pick_key"]
            ts = str(record.get("ts") or "")
            first_ts.setdefault(key, ts)
            window_end = record.get("window_end")
            latest[key] = PickWait(
                pick_key=key,
                gate=record["gate"],
                message=str(record.get("message") or ""),
                ts=ts,
                since=first_ts[key],
                window_end=str(window_end) if window_end else None,
                record=record,
            )
    return PickWaitFold(latest=latest, malformed=malformed)


def open_waits(
    records: Iterable[PickRecord],
    submitted: Collection[tuple[str, str]],
    fold: PickWaitFold,
) -> dict[tuple[str, str], PickWait]:
    """The waits that are still TRUE: their pick is armed and the drain has not
    placed it (``submitted`` is ``picks.submitted_pick_keys``). Keyed on the
    queue's (ticker, identity token). A line for a pick that was placed,
    disarmed, refused or expired since is history, and is dropped here."""
    current: dict[tuple[str, str], PickWait] = {}
    for record in records:
        key = (record.ticker, record.token)
        if record.status != STATUS_ARMED or key in submitted:
            continue
        wait = fold.latest.get(f"{record.ticker}:{record.token}")
        if wait is not None:
            current[key] = wait
    return current


def is_overdue(window_end: str | None, now: dt.datetime) -> bool:
    """``now`` is at or past ``window_end`` (an ISO timestamp; unreadable is not overdue).

    The drain expires such a pick on its next tick; a reader seeing one means
    the drain is not running (KILL, a dead chain, a stopped daemon)."""
    if not window_end:
        return False
    try:
        stamp = dt.datetime.fromisoformat(window_end)
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.UTC)
    return now >= stamp


__all__ = [
    "GATES",
    "GATE_CASH_FLOOR",
    "GATE_GROSS_CAP",
    "PickWait",
    "PickWaitFold",
    "append_wait",
    "is_overdue",
    "open_waits",
    "read_waits",
]

"""Append-only pick queue for the Saxo auto-manager.

One JSON line per `alphalens broker arm` under
~/.alphalens/broker_orders/picks.jsonl — the durable human-intent inbox the
control loop drains. Mirrors submission_log.py: the file is NEVER rewritten;
status is a recorded fact per line (T8 cohort discipline). Malformed/undated
lines are skipped; a missing file yields nothing.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PICKS_PATH = Path.home() / ".alphalens" / "broker_orders" / "picks.jsonl"

STATUS_ARMED = "armed"


@dataclass(frozen=True)
class Pick:
    ticker: str
    date: dt.date
    armed_ts: str
    status: str


def arm_pick(ticker: str, date: dt.date, *, path: Path | None = None) -> None:
    """Append one 'armed' intent line (append-only; never rewrites)."""
    target = path or DEFAULT_PICKS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ticker": ticker.upper(),
        "date": date.isoformat(),
        "armed_ts": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "status": STATUS_ARMED,
    }
    line = json.dumps(record, sort_keys=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def iter_picks(*, path: Path | None = None) -> Iterator[Pick]:
    """Yield parsed picks in append order. Malformed/undated lines skipped."""
    target = path or DEFAULT_PICKS_PATH
    if not target.exists():
        return
    with target.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            try:
                parsed_date = dt.date.fromisoformat(str(record["date"]))
            except (KeyError, ValueError):
                continue
            yield Pick(
                ticker=str(record.get("ticker", "")),
                date=parsed_date,
                armed_ts=str(record.get("armed_ts", "")),
                status=str(record.get("status", "")),
            )


__all__ = ["DEFAULT_PICKS_PATH", "STATUS_ARMED", "Pick", "arm_pick", "iter_picks"]

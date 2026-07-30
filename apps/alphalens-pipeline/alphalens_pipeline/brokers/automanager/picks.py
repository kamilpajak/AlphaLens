"""Append-only pick queue for the Saxo auto-manager.

One JSON line per `alphalens broker arm` under
~/.alphalens/broker_orders/picks.jsonl — the durable human-intent inbox the
control loop drains. Mirrors submission_log.py: the file is NEVER rewritten;
status is a recorded fact per line (T8 cohort discipline). Malformed/undated
lines are skipped; a missing file yields nothing.

Queue semantics: the LATEST status line per (ticker, date) wins. A terminal
``refused`` line (capacity/cap safety refusal) retires the pick so the drain
never retries it — re-arming via `alphalens broker arm` appends a fresh armed
line and is the explicit human path back.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PICKS_PATH = Path.home() / ".alphalens" / "broker_orders" / "picks.jsonl"

STATUS_ARMED = "armed"
STATUS_REFUSED = "refused"


@dataclass(frozen=True)
class Pick:
    ticker: str
    date: dt.date
    armed_ts: str
    status: str


def _append_record(record: dict[str, str], path: Path | None) -> None:
    """Append one JSON line (append-only; never rewrites)."""
    target = path or DEFAULT_PICKS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def arm_pick(ticker: str, date: dt.date, *, path: Path | None = None) -> None:
    """Append one 'armed' intent line (append-only; never rewrites)."""
    _append_record(
        {
            "ticker": ticker.upper(),
            "date": date.isoformat(),
            "armed_ts": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "status": STATUS_ARMED,
        },
        path,
    )


def mark_refused(ticker: str, date: dt.date, reason: str, *, path: Path | None = None) -> None:
    """Append one TERMINAL 'refused' line retiring the (ticker, date) pick.

    Written when safety.check refuses placement (open-legs cap / portfolio
    gross cap) — without it the armed pick retries every tick and self-places
    a stale brief signal days later once capacity frees. Re-arming via
    `alphalens broker arm` is the explicit human path back."""
    _append_record(
        {
            "ticker": ticker.upper(),
            "date": date.isoformat(),
            "refused_ts": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "reason": reason,
            "status": STATUS_REFUSED,
        },
        path,
    )


def _parse_record(raw_line: str) -> tuple[tuple[str, dt.date], dict] | None:
    """One well-formed status line -> ((TICKER, date), record); None if malformed."""
    line = raw_line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    try:
        parsed_date = dt.date.fromisoformat(str(record["date"]))
    except (KeyError, ValueError):
        return None
    return (str(record.get("ticker", "")).upper(), parsed_date), record


def iter_picks(*, path: Path | None = None) -> Iterator[Pick]:
    """Yield ARMED picks; the LATEST status line per (ticker, date) wins.

    Malformed/undated lines are skipped, and a pick whose latest line is
    non-armed (refused / cancelled / filled / expired) is never yielded — the
    control-loop drain places whatever this emits, so the ARMED filter lives
    here (defence in depth against re-placing a retired intent)."""
    target = path or DEFAULT_PICKS_PATH
    if not target.exists():
        return
    latest: dict[tuple[str, dt.date], dict] = {}
    with target.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            parsed = _parse_record(raw_line)
            if parsed is None:
                continue
            key, record = parsed
            latest[key] = record
    for (ticker, parsed_date), record in latest.items():
        if str(record.get("status", "")) != STATUS_ARMED:
            continue
        yield Pick(
            ticker=ticker,
            date=parsed_date,
            armed_ts=str(record.get("armed_ts", "")),
            status=str(record.get("status", "")),
        )


__all__ = [
    "DEFAULT_PICKS_PATH",
    "STATUS_ARMED",
    "STATUS_REFUSED",
    "Pick",
    "arm_pick",
    "iter_picks",
    "mark_refused",
]

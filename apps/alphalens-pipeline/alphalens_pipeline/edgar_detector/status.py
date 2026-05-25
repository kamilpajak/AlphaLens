from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def collect_status(
    queue_path: Path | str,
    digest_path: Path | str,
    seen_path: Path | str,
    budget_per_day: int = 5,
) -> dict[str, Any]:
    return {
        "queue": _queue_stats(Path(queue_path), budget_per_day),
        "digest": _digest_stats(Path(digest_path)),
        "seen_events": _seen_stats(Path(seen_path)),
    }


def _queue_stats(path: Path, budget_per_day: int) -> dict[str, Any]:
    empty = {
        "pending": 0,
        "in_progress": 0,
        "done_today": 0,
        "done_week": 0,
        "dead": 0,
        "budget_per_day": budget_per_day,
        "latest_done": None,
    }
    if not path.exists():
        return empty

    today = datetime.now(UTC).date().isoformat()
    week_ago = (datetime.now(UTC).date() - timedelta(days=7)).isoformat()

    conn = sqlite3.connect(str(path))
    try:
        conn.row_factory = sqlite3.Row
        counts = {
            row[0]: row[1]
            for row in conn.execute("SELECT status, COUNT(*) FROM candidates GROUP BY status")
        }
        done_today = conn.execute(
            "SELECT COUNT(*) FROM candidates WHERE status = 'done' AND DATE(finished_at) = ?",
            (today,),
        ).fetchone()[0]
        done_week = conn.execute(
            "SELECT COUNT(*) FROM candidates WHERE status = 'done' AND DATE(finished_at) >= ?",
            (week_ago,),
        ).fetchone()[0]
        latest = conn.execute(
            "SELECT ticker, decision, finished_at FROM candidates "
            "WHERE status = 'done' ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    return {
        "pending": counts.get("pending", 0),
        "in_progress": counts.get("in_progress", 0),
        "done_today": done_today,
        "done_week": done_week,
        "dead": counts.get("dead", 0),
        "budget_per_day": budget_per_day,
        "latest_done": dict(latest) if latest else None,
    }


def _digest_stats(path: Path) -> dict[str, Any]:
    empty = {"total": 0, "per_ticker": {}, "latest": None}
    if not path.exists():
        return empty

    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute("SELECT payload FROM digest_buffer ORDER BY id").fetchall()
    finally:
        conn.close()

    per_ticker: dict[str, int] = {}
    latest: dict[str, str] | None = None
    for (payload,) in rows:
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            continue
        ticker = data.get("ticker") or "?"
        per_ticker[ticker] = per_ticker.get(ticker, 0) + 1
        latest = {"ticker": ticker, "at": data.get("filed_at", "")}

    return {"total": len(rows), "per_ticker": per_ticker, "latest": latest}


def _seen_stats(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"total": 0}
    conn = sqlite3.connect(str(path))
    try:
        total = conn.execute("SELECT COUNT(*) FROM seen_events").fetchone()[0]
    finally:
        conn.close()
    return {"total": total}


def format_status(status: dict[str, Any]) -> str:
    q = status["queue"]
    d = status["digest"]
    s = status["seen_events"]

    lines = [
        "📊 EDGAR detector status",
        "",
        "Queue (auto-trigger)",
        f"  pending:      {q['pending']}",
        f"  in_progress:  {q['in_progress']}",
        f"  done today:   {q['done_today']} / {q['budget_per_day']} budget",
        f"  done week:    {q['done_week']}",
        f"  dead:         {q['dead']}",
    ]
    if q["latest_done"]:
        ld = q["latest_done"]
        lines.append(f"  latest done:  {ld['ticker']} → {ld['decision']} at {ld['finished_at']}")

    lines += ["", "Digest buffer", f"  total:        {d['total']}"]
    if d["per_ticker"]:
        for ticker, count in sorted(d["per_ticker"].items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {ticker:10s} {count}")

    lines += ["", "Dedup", f"  seen_events:  {s['total']}"]
    return "\n".join(lines)

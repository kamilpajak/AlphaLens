"""Scorer-acceptance analytics for the candidate queue.

Groups completed Layer 3 runs by source (i.e. which scorer produced the pick)
and computes decision distributions + accept rate. Used for paper-trade
validation of MomentumScorer vs EarlyStageScorer.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

ACCEPT_DECISIONS = ("BUY", "OVERWEIGHT")
DECISION_TYPES = ("BUY", "OVERWEIGHT", "HOLD", "UNDERWEIGHT", "SELL")


def _init_source_entry(src: str, finished_at: str) -> dict:
    return {
        "source": src,
        "total": 0,
        "buy_count": 0,
        "overweight_count": 0,
        "hold_count": 0,
        "underweight_count": 0,
        "sell_count": 0,
        "unknown_count": 0,
        "duration_sum": 0.0,
        "duration_n": 0,
        "cost_sum": 0.0,
        "cost_n": 0,
        "oldest_finished": finished_at,
        "newest_finished": finished_at,
    }


def _update_source_entry(entry: dict, row) -> None:
    entry["total"] += 1
    key = f"{(row['decision'] or '').lower()}_count"
    entry[key if key in entry else "unknown_count"] += 1
    if row["duration_sec"] is not None:
        entry["duration_sum"] += row["duration_sec"]
        entry["duration_n"] += 1
    if row["cost_usd"] is not None:
        entry["cost_sum"] += row["cost_usd"]
        entry["cost_n"] += 1
    entry["oldest_finished"] = min(entry["oldest_finished"], row["finished_at"])
    entry["newest_finished"] = max(entry["newest_finished"], row["finished_at"])


def _finalize_entry(entry: dict) -> dict:
    accept = entry["buy_count"] + entry["overweight_count"]
    entry["accept_rate"] = accept / entry["total"] if entry["total"] else 0.0
    entry["mean_duration_sec"] = (
        entry["duration_sum"] / entry["duration_n"] if entry["duration_n"] else None
    )
    entry["mean_cost_usd"] = entry["cost_sum"] / entry["cost_n"] if entry["cost_n"] else None
    for k in ("duration_sum", "duration_n", "cost_sum", "cost_n"):
        entry.pop(k, None)
    return entry


def compute_scorer_stats(db_path: Path | str, since_days: int = 30) -> list[dict]:
    """Return per-source summary of decisions in the last `since_days`.

    Only counts rows with `status = 'done'` (completed Layer 3 runs).
    Each row in the result has:
        source, total, {buy,overweight,hold,underweight,sell}_count,
        accept_rate (BUY+OVERWEIGHT / total), mean_duration_sec, mean_cost_usd,
        oldest_finished, newest_finished.
    """
    db_path = Path(db_path)
    cutoff = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT source, decision, duration_sec, cost_usd, finished_at
               FROM candidates
               WHERE status = 'done' AND finished_at >= ?""",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    by_source: dict[str, dict] = {}
    for r in rows:
        entry = by_source.setdefault(r["source"], _init_source_entry(r["source"], r["finished_at"]))
        _update_source_entry(entry, r)

    result = [_finalize_entry(e) for e in by_source.values()]
    result.sort(key=lambda e: e["source"])
    return result


def format_stats_table(stats: list[dict]) -> str:
    """Pretty-print scorer stats as a text table suitable for CLI output."""
    if not stats:
        return "No completed runs in window."

    lines = [
        f"{'source':<14} {'N':>4} {'accept':>8} {'BUY':>4} {'OW':>4} "
        f"{'HOLD':>5} {'UW':>4} {'SELL':>5} {'avg_dur':>8} {'avg_cost':>9}"
    ]
    lines.append("-" * len(lines[0]))
    for s in stats:
        accept_pct = s["accept_rate"] * 100
        dur = f"{s['mean_duration_sec']:.0f}s" if s["mean_duration_sec"] else "-"
        cost = f"${s['mean_cost_usd']:.3f}" if s["mean_cost_usd"] else "-"
        lines.append(
            f"{s['source']:<14} {s['total']:>4} {accept_pct:>7.1f}% "
            f"{s['buy_count']:>4} {s['overweight_count']:>4} {s['hold_count']:>5} "
            f"{s['underweight_count']:>4} {s['sell_count']:>5} {dur:>8} {cost:>9}"
        )
    return "\n".join(lines)

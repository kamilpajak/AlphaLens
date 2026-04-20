"""Scorer-acceptance analytics for the candidate queue.

Groups completed Layer 3 runs by source (i.e. which scorer produced the pick)
and computes decision distributions + accept rate. Used for paper-trade
validation of MomentumScorer vs EarlyStageScorer.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

ACCEPT_DECISIONS = ("BUY", "OVERWEIGHT")
DECISION_TYPES = ("BUY", "OVERWEIGHT", "HOLD", "UNDERWEIGHT", "SELL")


def compute_scorer_stats(db_path: Path | str, since_days: int = 30) -> list[dict]:
    """Return per-source summary of decisions in the last `since_days`.

    Only counts rows with `status = 'done'` (completed Layer 3 runs).
    Each row in the result has:
        source, total, {buy,overweight,hold,underweight,sell}_count,
        accept_rate (BUY+OVERWEIGHT / total), mean_duration_sec, mean_cost_usd,
        oldest_finished, newest_finished.
    """
    db_path = Path(db_path)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()

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
        src = r["source"]
        entry = by_source.setdefault(src, {
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
            "oldest_finished": r["finished_at"],
            "newest_finished": r["finished_at"],
        })
        entry["total"] += 1
        dec = (r["decision"] or "").upper()
        key = f"{dec.lower()}_count"
        if key in entry:
            entry[key] += 1
        else:
            entry["unknown_count"] += 1
        if r["duration_sec"] is not None:
            entry["duration_sum"] += r["duration_sec"]
            entry["duration_n"] += 1
        if r["cost_usd"] is not None:
            entry["cost_sum"] += r["cost_usd"]
            entry["cost_n"] += 1
        entry["oldest_finished"] = min(entry["oldest_finished"], r["finished_at"])
        entry["newest_finished"] = max(entry["newest_finished"], r["finished_at"])

    result = []
    for src, e in by_source.items():
        accept = e["buy_count"] + e["overweight_count"]
        e["accept_rate"] = accept / e["total"] if e["total"] else 0.0
        e["mean_duration_sec"] = (
            e["duration_sum"] / e["duration_n"] if e["duration_n"] else None
        )
        e["mean_cost_usd"] = e["cost_sum"] / e["cost_n"] if e["cost_n"] else None
        # Drop intermediate aggregation fields from the public dict
        for k in ("duration_sum", "duration_n", "cost_sum", "cost_n"):
            e.pop(k, None)
        result.append(e)

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

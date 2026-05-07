"""History of Layer 2b daily runs — SQLite store for the monitoring dashboard.

Every `themed screen` run is recorded here (run metadata + picks). The dashboard
(`alphalens archive themed status`) reads from this store and computes rolling metrics:
- Theme HHI trend (detect single-theme drift)
- Top-N persistence (which names sit in top-5 for > X days)
- Turnover (day-over-day flow of names)
- Error/skip rate (whether launchd has a problem)

Intentionally separate from `~/.alphalens/candidates.db` (the queue to Layer 3) —
monitoring is a different concern than pipeline execution.

The `momentum_score` column in `themed_picks` holds the canonical score —
`MomentumScorer` emits it directly, while `EarlyStageScorer` emits
`early_stage_score` which the pipeline renames to `momentum_score` (see
`themed/pipeline.py`) so downstream code (reporter, history, to_candidates)
stays scorer-agnostic.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd


def default_history_path() -> Path:
    root = Path.home() / ".alphalens"
    root.mkdir(parents=True, exist_ok=True)
    return root / "themed_history.db"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS themed_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    run_timestamp_utc TEXT NOT NULL,
    config_json TEXT NOT NULL,
    universe_size INTEGER NOT NULL,
    scored_count INTEGER NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS themed_picks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES themed_runs(run_id),
    rank INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    momentum_score REAL NOT NULL,
    themes TEXT NOT NULL,
    weight REAL,
    weighting_scheme TEXT,
    metric_breakdown_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_picks_run ON themed_picks(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_date ON themed_runs(run_date);
"""


@dataclass(frozen=True)
class RunRecord:
    run_id: int
    run_date: date
    run_timestamp_utc: datetime
    universe_size: int
    scored_count: int
    error: str | None


class ThemedHistoryStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else default_history_path()
        self._ensure_schema()

    def _ensure_schema(self):
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record_run(
        self,
        picks_df: pd.DataFrame,
        config: dict,
        universe_size: int,
        run_date: date | None = None,
        error: str | None = None,
        weighting_scheme: str = "equal",
        weights: list[float] | None = None,
    ) -> int:
        """Persist a daily `themed screen` run. Returns the new run_id."""
        run_date = run_date or datetime.now(UTC).date()
        now = datetime.now(UTC)
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO themed_runs
                   (run_date, run_timestamp_utc, config_json, universe_size, scored_count, error)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    run_date.isoformat(),
                    now.isoformat(),
                    json.dumps(config, default=str),
                    int(universe_size),
                    len(picks_df),
                    error,
                ),
            )
            run_id = cur.lastrowid
            if picks_df.empty:
                return run_id
            for rank_idx, (_, row) in enumerate(picks_df.iterrows(), start=1):
                themes = row.get("themes") or []
                if isinstance(themes, (list, tuple)):
                    themes_str = ",".join(themes)
                else:
                    themes_str = str(themes) if themes else ""
                breakdown = {
                    k: float(row[k])
                    for k in row.index
                    if k not in {"ticker", "momentum_score", "themes"}
                    and pd.notna(row.get(k))
                    and isinstance(row[k], (int, float))
                }
                weight = weights[rank_idx - 1] if weights else None
                c.execute(
                    """INSERT INTO themed_picks
                       (run_id, rank, ticker, momentum_score, themes, weight,
                        weighting_scheme, metric_breakdown_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id,
                        rank_idx,
                        str(row["ticker"]),
                        float(row["momentum_score"]),
                        themes_str,
                        weight,
                        weighting_scheme,
                        json.dumps(breakdown),
                    ),
                )
            return run_id

    def recent_runs(self, days: int = 30) -> list[RunRecord]:
        """Return the most recent N days of runs (descending chronological)."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT run_id, run_date, run_timestamp_utc, universe_size,
                          scored_count, error
                   FROM themed_runs
                   ORDER BY run_date DESC, run_id DESC
                   LIMIT ?""",
                (days,),
            ).fetchall()
        return [
            RunRecord(
                run_id=r["run_id"],
                run_date=date.fromisoformat(r["run_date"]),
                run_timestamp_utc=datetime.fromisoformat(r["run_timestamp_utc"]),
                universe_size=r["universe_size"],
                scored_count=r["scored_count"],
                error=r["error"],
            )
            for r in rows
        ]

    def picks_for_run(self, run_id: int) -> pd.DataFrame:
        with self._conn() as c:
            rows = c.execute(
                """SELECT rank, ticker, momentum_score, themes, weight, weighting_scheme
                   FROM themed_picks WHERE run_id = ? ORDER BY rank""",
                (run_id,),
            ).fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([dict(r) for r in rows])

    def picks_timeline(self, days: int = 30) -> pd.DataFrame:
        """DataFrame of all picks from the last N days (joined with run_date)."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT r.run_date, r.run_id, p.rank, p.ticker, p.momentum_score,
                          p.themes, p.weight, p.weighting_scheme
                   FROM themed_runs r
                   JOIN themed_picks p ON p.run_id = r.run_id
                   WHERE r.run_date >= date('now', ? )
                   ORDER BY r.run_date DESC, p.rank ASC""",
                (f"-{days} days",),
            ).fetchall()
        if not rows:
            return pd.DataFrame(
                columns=[
                    "run_date",
                    "run_id",
                    "rank",
                    "ticker",
                    "momentum_score",
                    "themes",
                    "weight",
                    "weighting_scheme",
                ]
            )
        return pd.DataFrame([dict(r) for r in rows])

    def count_runs(self) -> int:
        with self._conn() as c:
            row = c.execute("SELECT COUNT(*) AS c FROM themed_runs").fetchone()
        return int(row["c"])


def compute_staleness(timeline: pd.DataFrame, top_n: int = 5) -> pd.DataFrame:
    """Per-ticker staleness = consecutive days the name has been in top-N.

    Returns a DataFrame with columns: ticker, consecutive_days, last_rank.
    """
    if timeline.empty:
        return pd.DataFrame(columns=["ticker", "consecutive_days", "last_rank"])
    tn = timeline[timeline["rank"] <= top_n]
    tn = tn.sort_values(["run_date", "rank"], ascending=[False, True])
    stale = (
        tn.groupby("ticker")
        .agg(
            consecutive_days=("run_date", "nunique"),
            last_rank=("rank", "first"),
        )
        .reset_index()
    )
    return stale.sort_values("consecutive_days", ascending=False)


def compute_turnover_by_day(timeline: pd.DataFrame, top_n: int = 5) -> pd.DataFrame:
    """Per-day turnover = fraction of names that change vs the previous run."""
    if timeline.empty:
        return pd.DataFrame(columns=["run_date", "turnover"])
    tn = timeline[timeline["rank"] <= top_n]
    by_day = tn.groupby("run_date")["ticker"].apply(set).sort_index()
    records = []
    prev = None
    for run_date, names in by_day.items():
        if prev is None:
            records.append({"run_date": run_date, "turnover": 0.0})
        else:
            removed = prev - names
            records.append({"run_date": run_date, "turnover": len(removed) / max(len(prev), 1)})
        prev = names
    return pd.DataFrame(records)


def compute_theme_hhi_by_day(timeline: pd.DataFrame, top_n: int = 5) -> pd.DataFrame:
    """Per-day HHI (sum of squared theme weights) over the top-N picks."""
    if timeline.empty:
        return pd.DataFrame(columns=["run_date", "hhi", "dominant_theme", "dominant_weight"])
    tn = timeline[timeline["rank"] <= top_n]
    records = []
    for run_date, group in tn.groupby("run_date"):
        theme_counts: dict[str, float] = {}
        total = 0.0
        for _, row in group.iterrows():
            themes = [t for t in str(row["themes"]).split(",") if t]
            if not themes:
                continue
            share = 1.0 / len(themes)
            for t in themes:
                theme_counts[t] = theme_counts.get(t, 0.0) + share
            total += 1.0
        if total == 0:
            records.append(
                {
                    "run_date": run_date,
                    "hhi": 0.0,
                    "dominant_theme": "",
                    "dominant_weight": 0.0,
                }
            )
            continue
        weights = {k: v / total for k, v in theme_counts.items()}
        hhi = sum(w * w for w in weights.values())
        dominant = max(weights, key=weights.get)
        records.append(
            {
                "run_date": run_date,
                "hhi": hhi,
                "dominant_theme": dominant,
                "dominant_weight": weights[dominant],
            }
        )
    return pd.DataFrame(records).sort_values("run_date")

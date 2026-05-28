"""SQLite ledger for the paper-trade harness.

Single file: ``~/.alphalens/paper_ledger.db``. Schema declared idempotently
via ``CREATE TABLE IF NOT EXISTS`` so every callsite that opens the ledger
also self-heals on a fresh checkout (no separate migration step).

PR 2 populates ``plans`` / ``plan_entries`` / ``plan_exits`` / ``shadow_log``.
PR 3 will add ``orders`` / ``fills`` / ``exits``. Keep the schema additive —
the harness rebuilds the analysis report off this DB end-to-end, so dropping
a column would invalidate the record.

PR 3 forward-compat note (zen review §2 follow-up): every new child table
that hangs off ``plans`` (orders, fills, exits) MUST declare
``FOREIGN KEY (plan_id) REFERENCES plans(plan_id) ON DELETE CASCADE``. The
``_delete_existing_for_date`` helper only deletes from ``plans`` +
``shadow_log``; without CASCADE, a ``--force`` rerun would orphan order /
fill rows and silently corrupt the ledger. ``plan_entries`` and
``plan_exits`` already do this correctly — extend the same pattern.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Schema versioning lives inline. The planner pins ``brief_trade_setup``'s
# schema separately (sizing.compute_setup_plan); here we version OUR ledger
# layout. A future migration would bump this + add an ALTER TABLE block.
# Bumped to 2 (2026-05-28): v1 used per-candidate ``effective_size_pct``
# cap, v2 uses ``scale_factor`` (daily global) + ``final_size_pct``
# (suggested × scale). Per the project's "no backward compat" doctrine
# nothing migrates — the development DB is regenerated.
LEDGER_SCHEMA_VERSION = 2


_SCHEMA_DDL = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plans (
        plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
        brief_date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        theme TEXT NOT NULL,
        planned_at TEXT NOT NULL,
        suggested_size_pct REAL NOT NULL,    -- raw from brief_trade_setup
        scale_factor REAL NOT NULL,          -- v2 daily global scale (memo §2.3)
        final_size_pct REAL NOT NULL,        -- suggested × scale_factor
        paper_equity REAL NOT NULL,
        total_notional REAL NOT NULL,        -- final_size_pct/100 × equity
        gross_notional REAL NOT NULL,
        disaster_stop REAL NOT NULL,
        order_ttl_days INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('PLANNED', 'BLOCKED', 'SKIPPED')),
        block_reason TEXT,
        UNIQUE(brief_date, ticker)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plan_entries (
        entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER NOT NULL,
        tier_index INTEGER NOT NULL,
        limit_price REAL NOT NULL,
        qty INTEGER NOT NULL,
        alloc_pct REAL NOT NULL,
        tag TEXT NOT NULL,
        FOREIGN KEY (plan_id) REFERENCES plans(plan_id) ON DELETE CASCADE,
        UNIQUE(plan_id, tier_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plan_exits (
        exit_id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER NOT NULL,
        tranche_index INTEGER NOT NULL,
        target_price REAL NOT NULL,
        tranche_pct REAL NOT NULL,
        r_multiple REAL NOT NULL,
        tag TEXT NOT NULL,
        FOREIGN KEY (plan_id) REFERENCES plans(plan_id) ON DELETE CASCADE,
        UNIQUE(plan_id, tranche_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shadow_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        brief_date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        theme TEXT NOT NULL,
        reason TEXT NOT NULL,
        details_json TEXT NOT NULL,
        logged_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_plans_brief_date ON plans(brief_date)",
    "CREATE INDEX IF NOT EXISTS ix_shadow_brief_date ON shadow_log(brief_date)",
    "CREATE INDEX IF NOT EXISTS ix_shadow_reason ON shadow_log(reason)",
)


def _connect(path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with sensible defaults for this workload."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_ledger(path: Path) -> None:
    """Create the schema (idempotent). Called by every public function that
    opens the ledger so a freshly-deleted file self-heals on the next run.
    """
    with _connect(path) as conn:
        for stmt in _SCHEMA_DDL:
            conn.execute(stmt)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
            ("schema_version", str(LEDGER_SCHEMA_VERSION)),
        )


@contextmanager
def open_ledger(path: Path):
    """Context manager yielding a ready-to-use connection.

    The schema is created on entry (idempotent), so first-run + every-run
    cases share one code path. Auto-commit mode (``isolation_level=None``)
    keeps DDL + INSERT statements immediately durable; the caller wraps
    multi-row inserts in an explicit transaction via ``conn.execute("BEGIN")``.
    """
    init_ledger(path)
    conn = _connect(path)
    try:
        yield conn
    finally:
        conn.close()


@dataclass(frozen=True)
class PlanRow:
    """Minimal projection of a row in ``plans``. Used by the planner write
    helper to surface what got persisted; readers should query the DB
    directly for analysis."""

    plan_id: int
    brief_date: str
    ticker: str
    status: str
    total_notional: float
    gross_notional: float


def insert_planned(
    conn: sqlite3.Connection,
    *,
    brief_date: dt.date,
    ticker: str,
    theme: str,
    planned_at: dt.datetime,
    suggested_size_pct: float,
    scale_factor: float,
    final_size_pct: float,
    paper_equity: float,
    total_notional: float,
    gross_notional: float,
    disaster_stop: float,
    order_ttl_days: int,
    tiers: Iterable[tuple[int, float, int, float, str]],
    tp_tranches: Iterable[tuple[int, float, float, float, str]],
) -> PlanRow:
    """Insert a fully-planned candidate + its tier rows + TP-tranche rows.

    All writes happen inside one transaction so partial-write states are not
    visible to readers. ``tiers`` rows: ``(tier_index, limit_price, qty,
    alloc_pct, tag)``. ``tp_tranches`` rows: ``(tranche_index, target_price,
    tranche_pct, r_multiple, tag)``.
    """
    conn.execute("BEGIN")
    try:
        cur = conn.execute(
            """
            INSERT INTO plans(
                brief_date, ticker, theme, planned_at,
                suggested_size_pct, scale_factor, final_size_pct, paper_equity,
                total_notional, gross_notional, disaster_stop,
                order_ttl_days, status, block_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PLANNED', NULL)
            """,
            (
                brief_date.isoformat(),
                ticker,
                theme,
                planned_at.isoformat(),
                suggested_size_pct,
                scale_factor,
                final_size_pct,
                paper_equity,
                total_notional,
                gross_notional,
                disaster_stop,
                order_ttl_days,
            ),
        )
        plan_id = int(cur.lastrowid)
        conn.executemany(
            """INSERT INTO plan_entries(plan_id, tier_index, limit_price, qty, alloc_pct, tag)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(plan_id, *row) for row in tiers],
        )
        conn.executemany(
            """INSERT INTO plan_exits(plan_id, tranche_index, target_price, tranche_pct,
                                       r_multiple, tag)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(plan_id, *row) for row in tp_tranches],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return PlanRow(
        plan_id=plan_id,
        brief_date=brief_date.isoformat(),
        ticker=ticker,
        status="PLANNED",
        total_notional=total_notional,
        gross_notional=gross_notional,
    )


def insert_shadow(
    conn: sqlite3.Connection,
    *,
    brief_date: dt.date,
    ticker: str,
    theme: str,
    reason: str,
    details: dict[str, Any] | None = None,
    logged_at: dt.datetime | None = None,
) -> int:
    """Append a shadow-log entry. ``reason`` is a short tag
    (``same_ticker_open`` / ``gross_cap_block`` / ``not_verified`` /
    ``no_trade_setup`` / ``unplannable_setup``); ``details`` is a free-form
    JSON blob captured for retrospective analysis without rerunning the
    pipeline.
    """
    logged_at = logged_at or dt.datetime.now(dt.UTC)
    cur = conn.execute(
        """INSERT INTO shadow_log(brief_date, ticker, theme, reason, details_json, logged_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            brief_date.isoformat(),
            ticker,
            theme,
            reason,
            json.dumps(details or {}, sort_keys=True, default=str),
            logged_at.isoformat(),
        ),
    )
    return int(cur.lastrowid)


def count_plans_for_date(conn: sqlite3.Connection, brief_date: dt.date) -> int:
    cur = conn.execute("SELECT COUNT(*) FROM plans WHERE brief_date = ?", (brief_date.isoformat(),))
    return int(cur.fetchone()[0])


def count_shadow_for_date(conn: sqlite3.Connection, brief_date: dt.date) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) FROM shadow_log WHERE brief_date = ?", (brief_date.isoformat(),)
    )
    return int(cur.fetchone()[0])


def fetch_plans_for_date(conn: sqlite3.Connection, brief_date: dt.date) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM plans WHERE brief_date = ? ORDER BY plan_id", (brief_date.isoformat(),)
    )
    return list(cur.fetchall())


def fetch_shadow_for_date(conn: sqlite3.Connection, brief_date: dt.date) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM shadow_log WHERE brief_date = ? ORDER BY log_id",
        (brief_date.isoformat(),),
    )
    return list(cur.fetchall())


__all__ = [
    "LEDGER_SCHEMA_VERSION",
    "PlanRow",
    "count_plans_for_date",
    "count_shadow_for_date",
    "fetch_plans_for_date",
    "fetch_shadow_for_date",
    "init_ledger",
    "insert_planned",
    "insert_shadow",
    "open_ledger",
]

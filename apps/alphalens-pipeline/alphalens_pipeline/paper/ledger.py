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
import fcntl
import json
import logging
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Schema versioning lives inline. The planner pins ``brief_trade_setup``'s
# schema separately (sizing.compute_setup_plan); here we version OUR ledger
# layout. A future migration would bump this + add an ALTER TABLE block.
# Bumped to 3 (2026-05-28, PR 3): added orders, fills, exit_orders tables
# for the submitter + reconciler. All three reference plans(plan_id) ON
# DELETE CASCADE so --force re-runs cleanly cascade away the dependent
# rows (per zen review forward-note 2026-05-28).
# v1: effective_size_pct cap. v2: scale_factor + final_size_pct. v3:
# orders/fills/exits. v4: per-row account column on plans + orders
# (2026-05-28) so a single canonical ledger file can host orders from
# both Alpaca paper accounts ('main' + 'test') without collision when
# the reconciler queries Alpaca by order_id. Per "no backward compat"
# doctrine no migration code lives here — dev DB regenerated; existing
# operator ledger files migrated via the runbook in PR #279 description.
# v5: per-row platform column on plans + orders (issue #388) so a
# single canonical ledger can host orders from >1 paper-trading
# platform. Separate axis from account. Runbook-only migration (ALTER
# TABLE ... ADD COLUMN platform) per the v4 precedent — no migration
# code here.
LEDGER_SCHEMA_VERSION = 5

# Valid values for the per-row ``account`` column. Mirrors the Alpaca-
# client profile names so the planner / submitter / reconciler can pass
# the same string straight through. Application-level enforcement; the
# CHECK constraint on the schema is informational (SQLite's ALTER TABLE
# can't add CHECK retroactively so operator-migrated DBs may lack it).
VALID_ACCOUNTS = frozenset({"main", "test"})

# Valid values for the per-row `platform` column. SEPARATE axis from
# `account` (credential profile WITHIN a platform). Only 'alpaca' today.
# Application-level enforcement; the CHECK is informational (see
# VALID_ACCOUNTS note). Mirrors paper.broker.VALID_PLATFORMS.
VALID_PLATFORMS = frozenset({"alpaca"})


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
        -- v4: which Alpaca paper account this plan was sized + submitted
        -- against. 'main' = ALPACA_API_KEY/SECRET; 'test' = ALPACA_TEST_*.
        account TEXT NOT NULL DEFAULT 'main' CHECK(account IN ('main', 'test')),
        -- v5: paper-trading platform (issue #388). Separate axis from
        -- account. Only 'alpaca' today; CHECK informational, real
        -- enforcement is app-level (VALID_PLATFORMS at insert).
        platform TEXT NOT NULL DEFAULT 'alpaca' CHECK(platform IN ('alpaca')),
        UNIQUE(brief_date, ticker, account, platform)
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
    # v3: entry orders submitted to Alpaca after `paper submit`. One row
    # per submitted Alpaca order; ``alpaca_order_id`` is the SDK-returned
    # uuid so the reconciler can look it up by id. ``status`` tracks the
    # canonical lifecycle. ``side`` distinguishes BUY entries from SELL
    # exits (TP / SL / time-stop) so a single query against this table
    # covers both.
    """
    CREATE TABLE IF NOT EXISTS orders (
        order_id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER NOT NULL,
        alpaca_order_id TEXT NOT NULL UNIQUE,
        side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
        order_kind TEXT NOT NULL CHECK(order_kind IN ('ENTRY', 'TP', 'SL', 'TIME_STOP')),
        tier_index INTEGER,                 -- entry tier index for ENTRY orders, NULL otherwise
        tranche_index INTEGER,              -- TP tranche index for TP orders, NULL otherwise
        order_type TEXT NOT NULL CHECK(order_type IN ('LIMIT', 'STOP', 'MARKET')),
        qty INTEGER NOT NULL,
        limit_price REAL,                   -- LIMIT orders only
        stop_price REAL,                    -- STOP orders only
        time_in_force TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('SUBMITTED', 'PARTIALLY_FILLED', 'FILLED',
                                              'CANCELED', 'REJECTED', 'EXPIRED')),
        submitted_at TEXT NOT NULL,
        last_updated_at TEXT NOT NULL,
        -- v4: routing tag. Reconciler MUST filter on this when polling
        -- Alpaca — TEST account UUIDs would 404 against MAIN client.
        account TEXT NOT NULL DEFAULT 'main' CHECK(account IN ('main', 'test')),
        -- v5: paper-trading platform (issue #388). See plans.platform.
        platform TEXT NOT NULL DEFAULT 'alpaca' CHECK(platform IN ('alpaca')),
        FOREIGN KEY (plan_id) REFERENCES plans(plan_id) ON DELETE CASCADE
    )
    """,
    # v3: fills events. One row per fill (an order may produce several
    # rows for partial fills). The reconciler appends rows by walking
    # Alpaca's order activity stream; downstream R-multiple math
    # aggregates by ``order_id``.
    """
    CREATE TABLE IF NOT EXISTS fills (
        fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        alpaca_fill_id TEXT NOT NULL UNIQUE,
        qty INTEGER NOT NULL,
        price REAL NOT NULL,
        filled_at TEXT NOT NULL,
        FOREIGN KEY (order_id) REFERENCES orders(order_id) ON DELETE CASCADE
    )
    """,
    # v3: position lifecycle exits — a single row per plan summarising
    # how it left the harness. ``exit_kind`` mirrors the design memo §3
    # ('TP_HIT', 'SL_HIT', 'TIME_STOP_HIT', 'UNFILLED'). Written by the
    # reconciler when the position closes; one row per plan_id ever.
    """
    CREATE TABLE IF NOT EXISTS plan_outcomes (
        outcome_id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER NOT NULL UNIQUE,
        exit_kind TEXT NOT NULL CHECK(
            exit_kind IN ('TP_HIT', 'SL_HIT', 'TIME_STOP_HIT', 'UNFILLED', 'PARTIAL_TP')
        ),
        first_fill_at TEXT,                  -- earliest fill timestamp; NULL if never filled
        last_exit_at TEXT,                   -- latest exit timestamp; NULL if still open / unfilled
        blended_entry_price REAL,            -- qty-weighted across entry fills
        blended_exit_price REAL,             -- qty-weighted across exit fills
        realized_r_multiple REAL,            -- (exit − entry) / (entry − disaster_stop)
        closed_at TEXT NOT NULL,
        FOREIGN KEY (plan_id) REFERENCES plans(plan_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_plans_brief_date ON plans(brief_date)",
    "CREATE INDEX IF NOT EXISTS ix_shadow_brief_date ON shadow_log(brief_date)",
    "CREATE INDEX IF NOT EXISTS ix_shadow_reason ON shadow_log(reason)",
    "CREATE INDEX IF NOT EXISTS ix_orders_plan_id ON orders(plan_id)",
    "CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status)",
    "CREATE INDEX IF NOT EXISTS ix_orders_kind_status ON orders(order_kind, status)",
    "CREATE INDEX IF NOT EXISTS ix_orders_account_status ON orders(account, status)",
    "CREATE INDEX IF NOT EXISTS ix_plans_account_brief ON plans(account, brief_date)",
    "CREATE INDEX IF NOT EXISTS ix_fills_order_id ON fills(order_id)",
)


def _connect(path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with sensible defaults for this workload.

    ``timeout=60.0``: when the fcntl.flock advisory lock falls back on
    NFS / FUSE filesystems (warn + continue path in ``open_ledger``), the
    only remaining serialisation is SQLite's busy-wait. The default 5s
    is too short for a reconciler pass that polls Alpaca per open order
    (~1s × N orders) — bump to 60s so the fallback path doesn't crash a
    second writer with ``OperationalError: database is locked``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _assert_v5_platform_columns(conn: sqlite3.Connection) -> None:
    """Fail-fast if an operator-migrated ledger predates the v5 platform
    column (runbook ALTER not yet run). Turns a cryptic OperationalError
    mid-insert into a clear deploy-guard. See issue #388 + the v4->v5
    runbook in the PR description."""
    for table in ("plans", "orders"):
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "platform" not in cols:
            raise RuntimeError(
                f"ledger schema v5: '{table}' is missing the 'platform' column. "
                "Run the v4->v5 runbook migration before deploying: "
                f"ALTER TABLE {table} ADD COLUMN platform TEXT NOT NULL DEFAULT 'alpaca'"
            )


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
        # Deploy-ordering guard: a fresh DB passes (CREATE TABLE just made
        # the column); only an operator-migrated v4 file where the runbook
        # ALTER has not yet run fails here, with a clear instruction.
        _assert_v5_platform_columns(conn)


@contextmanager
def open_ledger(path: Path):
    """Context manager yielding a ready-to-use connection, guarded by a
    POSIX advisory file lock so two processes cannot both write at once.

    The schema is created on entry (idempotent), so first-run + every-run
    cases share one code path. Auto-commit mode (``isolation_level=None``)
    keeps DDL + INSERT statements immediately durable; the caller wraps
    multi-row inserts in an explicit transaction via ``conn.execute("BEGIN")``.

    Concurrency model (memo planner docstring + zen second-round review):
    the ledger is single-writer. SQLite's WAL journal allows concurrent
    readers but serialises writers; before PR 3's reconciler cron was
    added the planner was the only writer, but now ``alphalens paper
    submit`` + ``alphalens paper reconcile`` may both run from cron AND
    an operator may invoke ``alphalens paper plan`` manually. An advisory
    flock on ``{db_path}.lock`` arbitrates. The lock is held for the
    duration of the context, released on exit (close of the lockfile
    handle). Tests run sequentially in one process so the lock is a
    no-op there; this matters for production cron + manual overlap.
    """
    init_ledger(path)
    lock_path = path.with_name(path.name + ".lock")
    lock_handle = open(lock_path, "w")  # noqa: SIM115 — managed by try/finally below
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
    except OSError as exc:  # pragma: no cover - platform fallback
        # Some filesystems (NFS, FUSE) don't support flock; log + proceed
        # unguarded. Single-writer assumption is documented either way.
        logger.warning("ledger advisory lock unavailable (%s): %s", lock_path, exc)
    conn = _connect(path)
    try:
        yield conn
    finally:
        conn.close()
        import contextlib

        with contextlib.suppress(OSError):  # pragma: no cover - filesystem fallback
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
        lock_handle.close()


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


# NOSONAR S107: the kwargs-only signature mirrors the ``plans`` schema
# 1:1 by design — each column maps to one named parameter so a schema
# bump (v2 / v3 / v4 added rows of columns) is a mechanical add here
# without ambiguity. Refactoring into a dataclass arg would push the
# same column-count into the dataclass surface for no DX win and would
# break every callsite for cosmetic parameter-count compliance.
def insert_planned(  # NOSONAR S107
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
    account: str = "main",
    platform: str = "alpaca",
) -> PlanRow:
    """Insert a fully-planned candidate + its tier rows + TP-tranche rows.

    All writes happen inside one transaction so partial-write states are not
    visible to readers. ``tiers`` rows: ``(tier_index, limit_price, qty,
    alloc_pct, tag)``. ``tp_tranches`` rows: ``(tranche_index, target_price,
    tranche_pct, r_multiple, tag)``.

    ``account`` (v4): which Alpaca paper account this plan is sized against
    + will submit to. Must be one of :data:`VALID_ACCOUNTS`.
    """
    if account not in VALID_ACCOUNTS:
        raise ValueError(f"unknown account={account!r}, expected one of {sorted(VALID_ACCOUNTS)}")
    if platform not in VALID_PLATFORMS:
        raise ValueError(
            f"unknown platform={platform!r}, expected one of {sorted(VALID_PLATFORMS)}"
        )
    conn.execute("BEGIN")
    try:
        cur = conn.execute(
            """
            INSERT INTO plans(
                brief_date, ticker, theme, planned_at,
                suggested_size_pct, scale_factor, final_size_pct, paper_equity,
                total_notional, gross_notional, disaster_stop,
                order_ttl_days, status, block_reason, account, platform
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PLANNED', NULL, ?, ?)
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
                account,
                platform,
            ),
        )
        # ``lastrowid`` is typed Optional[int] by the stdlib stubs, but
        # SQLite always populates it after an AUTOINCREMENT INSERT we just
        # ran inside an open transaction. The None branch is unreachable
        # here; the cast to int is for the pyright contract, not safety.
        last = cur.lastrowid
        if last is None:  # pragma: no cover - defensive against SDK contract drift
            raise RuntimeError("sqlite returned no lastrowid after INSERT into plans")
        plan_id = int(last)
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
    last = cur.lastrowid
    if last is None:  # pragma: no cover - defensive against SDK contract drift
        raise RuntimeError("sqlite returned no lastrowid after INSERT into shadow_log")
    return int(last)


def count_plans_for_date(conn: sqlite3.Connection, brief_date: dt.date) -> int:
    cur = conn.execute("SELECT COUNT(*) FROM plans WHERE brief_date = ?", (brief_date.isoformat(),))
    return int(cur.fetchone()[0])


def count_shadow_for_date(conn: sqlite3.Connection, brief_date: dt.date) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) FROM shadow_log WHERE brief_date = ?", (brief_date.isoformat(),)
    )
    return int(cur.fetchone()[0])


def fetch_plans_for_date(
    conn: sqlite3.Connection,
    brief_date: dt.date,
    *,
    account: str | None = None,
) -> list[sqlite3.Row]:
    """Plans for one brief date. ``account=None`` returns all accounts
    (audit / report path); pass ``'main'`` or ``'test'`` to scope the
    result to a single Alpaca paper account."""
    if account is None:
        cur = conn.execute(
            "SELECT * FROM plans WHERE brief_date = ? ORDER BY plan_id",
            (brief_date.isoformat(),),
        )
    else:
        if account not in VALID_ACCOUNTS:
            raise ValueError(
                f"unknown account={account!r}, expected one of {sorted(VALID_ACCOUNTS)}"
            )
        cur = conn.execute(
            "SELECT * FROM plans WHERE brief_date = ? AND account = ? ORDER BY plan_id",
            (brief_date.isoformat(), account),
        )
    return list(cur.fetchall())


def fetch_shadow_for_date(conn: sqlite3.Connection, brief_date: dt.date) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM shadow_log WHERE brief_date = ? ORDER BY log_id",
        (brief_date.isoformat(),),
    )
    return list(cur.fetchall())


# ----- v3: orders / fills / outcomes helpers (submitter + reconciler) -----


# NOSONAR S107: parameter count mirrors ``orders`` schema 1:1 — see
# ``insert_planned`` rationale above.
def insert_order(  # NOSONAR S107
    conn: sqlite3.Connection,
    *,
    plan_id: int,
    alpaca_order_id: str,
    side: str,
    order_kind: str,
    order_type: str,
    qty: int,
    time_in_force: str,
    submitted_at: dt.datetime,
    tier_index: int | None = None,
    tranche_index: int | None = None,
    limit_price: float | None = None,
    stop_price: float | None = None,
    status: str = "SUBMITTED",
    account: str = "main",
    platform: str = "alpaca",
) -> int:
    """Persist a freshly submitted Alpaca order. Returns the local ``order_id``.

    Called by the submitter after each ``submit_limit_order`` /
    ``submit_stop_order`` / ``submit_market_order`` returns from the SDK.
    The reconciler walks ``orders`` joined with Alpaca state to detect
    status transitions.

    ``account`` (v4): the paper account the SDK call routed to. Reconciler
    + exit_manager MUST filter on this when picking which Alpaca client
    to poll; cross-account UUID lookups would 404.
    """
    if account not in VALID_ACCOUNTS:
        raise ValueError(f"unknown account={account!r}, expected one of {sorted(VALID_ACCOUNTS)}")
    if platform not in VALID_PLATFORMS:
        raise ValueError(
            f"unknown platform={platform!r}, expected one of {sorted(VALID_PLATFORMS)}"
        )
    cur = conn.execute(
        """INSERT INTO orders(plan_id, alpaca_order_id, side, order_kind,
                              tier_index, tranche_index, order_type, qty,
                              limit_price, stop_price, time_in_force, status,
                              submitted_at, last_updated_at, account, platform)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            plan_id,
            alpaca_order_id,
            side,
            order_kind,
            tier_index,
            tranche_index,
            order_type,
            qty,
            limit_price,
            stop_price,
            time_in_force,
            status,
            submitted_at.isoformat(),
            submitted_at.isoformat(),
            account,
            platform,
        ),
    )
    last = cur.lastrowid
    if last is None:  # pragma: no cover - defensive against SDK contract drift
        raise RuntimeError("sqlite returned no lastrowid after INSERT into orders")
    return int(last)


def update_order_status(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    status: str,
    last_updated_at: dt.datetime | None = None,
) -> None:
    """Move an order to a new lifecycle state. The reconciler calls this
    when Alpaca reports a transition that ``insert_order`` did not capture
    (e.g. SUBMITTED → PARTIALLY_FILLED → FILLED → CANCELED).
    """
    ts = last_updated_at or dt.datetime.now(dt.UTC)
    conn.execute(
        "UPDATE orders SET status = ?, last_updated_at = ? WHERE order_id = ?",
        (status, ts.isoformat(), order_id),
    )


def insert_fill(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    alpaca_fill_id: str,
    qty: int,
    price: float,
    filled_at: dt.datetime,
) -> int:
    """Append one fill event. ``alpaca_fill_id`` is the SDK's deduplicating
    identifier so re-running the reconciler is idempotent (UNIQUE blocks
    duplicate rows).
    """
    cur = conn.execute(
        """INSERT INTO fills(order_id, alpaca_fill_id, qty, price, filled_at)
           VALUES (?, ?, ?, ?, ?)""",
        (order_id, alpaca_fill_id, qty, price, filled_at.isoformat()),
    )
    last = cur.lastrowid
    if last is None:  # pragma: no cover - defensive against SDK contract drift
        raise RuntimeError("sqlite returned no lastrowid after INSERT into fills")
    return int(last)


def insert_plan_outcome(
    conn: sqlite3.Connection,
    *,
    plan_id: int,
    exit_kind: str,
    closed_at: dt.datetime,
    first_fill_at: dt.datetime | None = None,
    last_exit_at: dt.datetime | None = None,
    blended_entry_price: float | None = None,
    blended_exit_price: float | None = None,
    realized_r_multiple: float | None = None,
) -> int:
    """Record the final disposition of a plan. One row per plan_id ever
    (the UNIQUE constraint enforces this). Written by the reconciler when
    the position closes via TP / SL / time-stop / never-filled-then-
    cancelled paths.
    """
    cur = conn.execute(
        """INSERT INTO plan_outcomes(plan_id, exit_kind, first_fill_at, last_exit_at,
                                      blended_entry_price, blended_exit_price,
                                      realized_r_multiple, closed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            plan_id,
            exit_kind,
            first_fill_at.isoformat() if first_fill_at is not None else None,
            last_exit_at.isoformat() if last_exit_at is not None else None,
            blended_entry_price,
            blended_exit_price,
            realized_r_multiple,
            closed_at.isoformat(),
        ),
    )
    last = cur.lastrowid
    if last is None:  # pragma: no cover - defensive against SDK contract drift
        raise RuntimeError("sqlite returned no lastrowid after INSERT into plan_outcomes")
    return int(last)


def fetch_orders_for_plan(conn: sqlite3.Connection, plan_id: int) -> list[sqlite3.Row]:
    """All orders for a plan ordered by submission time. Used by the
    reconciler to compute blended entry/exit prices.

    No account filter — a plan_id is unique across accounts (the
    plans-side ``UNIQUE(brief_date, ticker, account, platform)`` constraint
    means one ticker on one day can produce one plan row per (account,
    platform) pair, each with its own plan_id; all orders for a given
    plan_id are by construction routed to the plan's account)."""
    cur = conn.execute(
        "SELECT * FROM orders WHERE plan_id = ? ORDER BY submitted_at, order_id",
        (plan_id,),
    )
    return list(cur.fetchall())


def fetch_open_orders(
    conn: sqlite3.Connection,
    *,
    account: str | None = None,
) -> list[sqlite3.Row]:
    """All orders not yet in a terminal state (SUBMITTED / PARTIALLY_FILLED).

    Reconciler uses this to know which Alpaca orders to poll. The
    ``account`` filter is REQUIRED for any reconcile pass — TEST account
    UUIDs would 404 against MAIN client and vice versa. ``account=None``
    returns the full open-order set across accounts (audit / report
    path)."""
    if account is None:
        cur = conn.execute(
            "SELECT * FROM orders WHERE status IN ('SUBMITTED', 'PARTIALLY_FILLED') "
            "ORDER BY submitted_at"
        )
    else:
        if account not in VALID_ACCOUNTS:
            raise ValueError(
                f"unknown account={account!r}, expected one of {sorted(VALID_ACCOUNTS)}"
            )
        cur = conn.execute(
            "SELECT * FROM orders WHERE status IN ('SUBMITTED', 'PARTIALLY_FILLED') "
            "AND account = ? ORDER BY submitted_at",
            (account,),
        )
    return list(cur.fetchall())


def fetch_outcome_for_plan(conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row | None:
    """The final disposition row for a plan, or None if it has not closed.

    Consumed by the feedback outcome-join (Track A v2): a NULL return means
    the plan is still open / unfilled-but-not-yet-cancelled, so the joined
    decision is left with NULL outcome columns until a later sweep finds the
    matured row (``plan_outcomes`` has UNIQUE(plan_id), so at most one)."""
    cur = conn.execute(
        "SELECT * FROM plan_outcomes WHERE plan_id = ?",
        (plan_id,),
    )
    return cur.fetchone()


def fetch_fills_for_order(conn: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM fills WHERE order_id = ? ORDER BY filled_at, fill_id",
        (order_id,),
    )
    return list(cur.fetchall())


__all__ = [
    "LEDGER_SCHEMA_VERSION",
    "VALID_ACCOUNTS",
    "VALID_PLATFORMS",
    "PlanRow",
    "count_plans_for_date",
    "count_shadow_for_date",
    "fetch_fills_for_order",
    "fetch_open_orders",
    "fetch_orders_for_plan",
    "fetch_outcome_for_plan",
    "fetch_plans_for_date",
    "fetch_shadow_for_date",
    "init_ledger",
    "insert_fill",
    "insert_order",
    "insert_plan_outcome",
    "insert_planned",
    "insert_shadow",
    "open_ledger",
    "update_order_status",
]

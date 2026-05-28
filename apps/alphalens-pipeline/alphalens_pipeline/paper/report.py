"""Read-only aggregation over the paper-trade SQLite ledger.

Surfaces what the operator needs to see after a few cycles of plan +
submit + reconcile have run: how many plans landed, how many entries
filled, what outcomes have closed, what the R-multiple distribution
looks like. No live Alpaca calls — every figure comes from the ledger.

The CLI wrapper is ``alphalens paper report`` (see
``alphalens_cli/commands/paper.py``). Schema-wise the report is purely a
projection of existing tables — no new columns, no migrations.

R-multiple statistics intentionally exclude outcomes whose
``realized_r_multiple`` is NULL (the UNFILLED branch in exit_manager
writes NULL when there were zero entry fills). Those candidates never
got exposure so including them as "zero R" would falsely deflate the
distribution.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from alphalens_pipeline.paper.ledger import VALID_ACCOUNTS, open_ledger


@dataclass(frozen=True)
class ReportSummary:
    """Aggregate counters + distribution stats across the report scope."""

    n_plans_planned: int
    n_plans_blocked: int
    n_shadowed: int
    shadow_by_reason: dict[str, int]
    n_entries_submitted: int
    n_entries_filled: int
    n_tp_orders: int
    n_sl_orders: int
    n_time_stop_orders: int
    n_fills: int
    n_outcomes: int
    outcomes_by_kind: dict[str, int]
    n_r_multiple_observations: int
    r_multiple_mean: float | None
    r_multiple_median: float | None
    r_multiple_stdev: float | None
    hit_rate: float | None  # TP_HIT / outcomes-with-fills


@dataclass(frozen=True)
class CandidateRow:
    """One row in the per-candidate table — current state of a plan."""

    plan_id: int
    brief_date: str
    ticker: str
    theme: str
    account: str
    status: str  # plans.status (PLANNED / BLOCKED / SKIPPED)
    entry_planned_qty: int  # sum of qty across plan_entries tier rows
    entry_filled_qty: int  # sum of fills.qty across ENTRY orders
    blended_entry_price: float | None
    exit_kind: str | None  # NULL until plan_outcome row written
    realized_r_multiple: float | None
    closed_at: str | None


@dataclass(frozen=True)
class Report:
    """Full report: aggregate summary + per-candidate detail rows."""

    summary: ReportSummary
    candidates: tuple[CandidateRow, ...]
    scope: dict = field(default_factory=dict)  # brief_date / account scope echo


# ----- internal query helpers (one per section so the SQL stays close
# to the dataclass field it populates) -----


def _count_plan_statuses(
    conn: sqlite3.Connection,
    *,
    brief_date: dt.date | None,
    account: str | None,
) -> tuple[int, int]:
    """Returns (n_planned, n_blocked). SKIPPED is currently not used by
    the planner but the schema allows it, so the count is implicit in
    the planned + blocked total."""
    where, params = _scope_where(brief_date=brief_date, account=account, alias="")
    cur = conn.execute(
        f"SELECT status, COUNT(*) FROM plans {where} GROUP BY status",  # nosec B608
        params,
    )
    counts = dict(cur.fetchall())
    return int(counts.get("PLANNED", 0)), int(counts.get("BLOCKED", 0))


def _shadow_breakdown(
    conn: sqlite3.Connection,
    *,
    brief_date: dt.date | None,
) -> tuple[int, dict[str, int]]:
    # shadow_log carries no account column (per ledger schema) — scope is
    # by brief_date only.
    if brief_date is None:
        cur = conn.execute("SELECT reason, COUNT(*) FROM shadow_log GROUP BY reason")
    else:
        cur = conn.execute(
            "SELECT reason, COUNT(*) FROM shadow_log WHERE brief_date = ? GROUP BY reason",
            (brief_date.isoformat(),),
        )
    by_reason = {row[0]: int(row[1]) for row in cur.fetchall()}
    return sum(by_reason.values()), by_reason


def _order_lifecycle_counts(
    conn: sqlite3.Connection,
    *,
    brief_date: dt.date | None,
    account: str | None,
) -> dict[str, int]:
    """Counts orders by (order_kind, status). The keys we surface are
    coarse — full status × kind matrix is overkill in the summary; the
    operator drills into per-candidate detail if they want it."""
    plans_where, plans_params = _scope_where(brief_date=brief_date, account=account, alias="p.")
    join = "JOIN plans p ON p.plan_id = o.plan_id"
    counts = {
        "entries_submitted": 0,
        "entries_filled": 0,
        "tp_orders": 0,
        "sl_orders": 0,
        "time_stop_orders": 0,
    }
    cur = conn.execute(
        f"""SELECT o.order_kind, o.status, COUNT(*)
            FROM orders o {join}
            {plans_where}
            GROUP BY o.order_kind, o.status""",  # nosec B608
        plans_params,
    )
    for kind, status, n_raw in cur.fetchall():
        n = int(n_raw)
        if kind == "ENTRY":
            counts["entries_submitted"] += n
            if status == "FILLED":
                counts["entries_filled"] += n
        elif kind == "TP":
            counts["tp_orders"] += n
        elif kind == "SL":
            counts["sl_orders"] += n
        elif kind == "TIME_STOP":
            counts["time_stop_orders"] += n
    return counts


def _count_fills(
    conn: sqlite3.Connection,
    *,
    brief_date: dt.date | None,
    account: str | None,
) -> int:
    plans_where, plans_params = _scope_where(brief_date=brief_date, account=account, alias="p.")
    cur = conn.execute(
        f"""SELECT COUNT(*) FROM fills f
            JOIN orders o ON o.order_id = f.order_id
            JOIN plans p ON p.plan_id = o.plan_id
            {plans_where}""",  # nosec B608
        plans_params,
    )
    return int(cur.fetchone()[0])


def _outcome_aggregates(
    conn: sqlite3.Connection,
    *,
    brief_date: dt.date | None,
    account: str | None,
) -> tuple[int, dict[str, int], list[float]]:
    """Returns (n_outcomes_total, by_kind, r_observations).

    ``r_observations`` excludes NULL R-multiples (UNFILLED outcomes) —
    statistics over those would conflate "no exposure" with "zero P&L"."""
    plans_where, plans_params = _scope_where(brief_date=brief_date, account=account, alias="p.")
    cur = conn.execute(
        f"""SELECT po.exit_kind, po.realized_r_multiple
            FROM plan_outcomes po
            JOIN plans p ON p.plan_id = po.plan_id
            {plans_where}""",  # nosec B608
        plans_params,
    )
    by_kind: Counter[str] = Counter()
    r_observations: list[float] = []
    for kind, r in cur.fetchall():
        by_kind[kind] += 1
        if r is not None:
            r_observations.append(float(r))
    return sum(by_kind.values()), dict(by_kind), r_observations


def _r_multiple_stats(
    r_observations: list[float],
) -> tuple[float | None, float | None, float | None]:
    """Mean / median / stdev over a list of realised R-multiples.
    Returns (None, None, None) when no observations exist (empty list);
    stdev is None when only one observation (statistics.stdev requires n>=2)."""
    if not r_observations:
        return None, None, None
    mean = statistics.fmean(r_observations)
    median = statistics.median(r_observations)
    stdev = statistics.stdev(r_observations) if len(r_observations) >= 2 else None
    return mean, median, stdev


def _hit_rate(by_kind: dict[str, int]) -> float | None:
    """TP_HIT / (outcomes that actually had entry fills). PARTIAL_TP and
    TIME_STOP_HIT count toward the denominator since exposure was taken;
    UNFILLED does not (no exposure ever materialised)."""
    with_fills = sum(by_kind.get(k, 0) for k in ("TP_HIT", "SL_HIT", "PARTIAL_TP", "TIME_STOP_HIT"))
    if with_fills == 0:
        return None
    return by_kind.get("TP_HIT", 0) / with_fills


def _candidate_rows(
    conn: sqlite3.Connection,
    *,
    brief_date: dt.date | None,
    account: str | None,
) -> tuple[CandidateRow, ...]:
    """One row per plan, joined with the rollup of entry fills and the
    optional plan_outcome. Ordered by plan_id so output is diff-stable."""
    plans_where, plans_params = _scope_where(brief_date=brief_date, account=account, alias="p.")
    cur = conn.execute(
        f"""SELECT
              p.plan_id, p.brief_date, p.ticker, p.theme, p.account, p.status,
              COALESCE((SELECT SUM(pe.qty) FROM plan_entries pe
                        WHERE pe.plan_id = p.plan_id), 0) AS entry_planned_qty,
              COALESCE((SELECT SUM(f.qty)
                        FROM fills f JOIN orders o ON o.order_id = f.order_id
                        WHERE o.plan_id = p.plan_id AND o.order_kind = 'ENTRY'), 0)
                  AS entry_filled_qty,
              (SELECT SUM(f.qty * f.price) / NULLIF(SUM(f.qty), 0)
                 FROM fills f JOIN orders o ON o.order_id = f.order_id
                 WHERE o.plan_id = p.plan_id AND o.order_kind = 'ENTRY')
                  AS blended_entry,
              po.exit_kind, po.realized_r_multiple, po.closed_at
            FROM plans p
            LEFT JOIN plan_outcomes po ON po.plan_id = p.plan_id
            {plans_where}
            ORDER BY p.plan_id""",  # nosec B608
        plans_params,
    )
    rows = []
    for r in cur.fetchall():
        rows.append(
            CandidateRow(
                plan_id=int(r["plan_id"]),
                brief_date=r["brief_date"],
                ticker=r["ticker"],
                theme=r["theme"],
                account=r["account"],
                status=r["status"],
                entry_planned_qty=int(r["entry_planned_qty"]),
                entry_filled_qty=int(r["entry_filled_qty"]),
                blended_entry_price=float(r["blended_entry"])
                if r["blended_entry"] is not None
                else None,
                exit_kind=r["exit_kind"],
                realized_r_multiple=(
                    float(r["realized_r_multiple"])
                    if r["realized_r_multiple"] is not None
                    else None
                ),
                closed_at=r["closed_at"],
            )
        )
    return tuple(rows)


def _scope_where(
    *,
    brief_date: dt.date | None,
    account: str | None,
    alias: str,
) -> tuple[str, tuple]:
    """Build a parameterised ``WHERE`` clause for the (date, account) scope.
    ``alias`` is the table prefix (``"p."`` for joined queries, ``""``
    for the bare ``plans`` query). Returns ("WHERE ...", params) or
    ("", ()) when no scope filters apply."""
    clauses = []
    params: list = []
    if brief_date is not None:
        clauses.append(f"{alias}brief_date = ?")
        params.append(brief_date.isoformat())
    if account is not None:
        if account not in VALID_ACCOUNTS:
            raise ValueError(
                f"unknown account={account!r}, expected one of {sorted(VALID_ACCOUNTS)}"
            )
        clauses.append(f"{alias}account = ?")
        params.append(account)
    if not clauses:
        return "", ()
    return "WHERE " + " AND ".join(clauses), tuple(params)


def build_report(
    ledger_path: Path,
    *,
    brief_date: dt.date | None = None,
    account: str | None = None,
) -> Report:
    """Build a Report from the ledger at ``ledger_path``.

    Scope is multiplicative: passing both ``brief_date`` and ``account``
    narrows to the intersection. Passing neither aggregates across the
    whole ledger.
    """
    with open_ledger(ledger_path) as conn:
        n_planned, n_blocked = _count_plan_statuses(conn, brief_date=brief_date, account=account)
        n_shadowed, shadow_by_reason = _shadow_breakdown(conn, brief_date=brief_date)
        lifecycle = _order_lifecycle_counts(conn, brief_date=brief_date, account=account)
        n_fills = _count_fills(conn, brief_date=brief_date, account=account)
        n_outcomes, outcomes_by_kind, r_observations = _outcome_aggregates(
            conn, brief_date=brief_date, account=account
        )
        candidates = _candidate_rows(conn, brief_date=brief_date, account=account)

    r_mean, r_median, r_stdev = _r_multiple_stats(r_observations)
    summary = ReportSummary(
        n_plans_planned=n_planned,
        n_plans_blocked=n_blocked,
        n_shadowed=n_shadowed,
        shadow_by_reason=shadow_by_reason,
        n_entries_submitted=lifecycle["entries_submitted"],
        n_entries_filled=lifecycle["entries_filled"],
        n_tp_orders=lifecycle["tp_orders"],
        n_sl_orders=lifecycle["sl_orders"],
        n_time_stop_orders=lifecycle["time_stop_orders"],
        n_fills=n_fills,
        n_outcomes=n_outcomes,
        outcomes_by_kind=outcomes_by_kind,
        n_r_multiple_observations=len(r_observations),
        r_multiple_mean=r_mean,
        r_multiple_median=r_median,
        r_multiple_stdev=r_stdev,
        hit_rate=_hit_rate(outcomes_by_kind),
    )

    return Report(
        summary=summary,
        candidates=candidates,
        scope={
            "brief_date": brief_date.isoformat() if brief_date else None,
            "account": account,
        },
    )


__all__ = [
    "CandidateRow",
    "Report",
    "ReportSummary",
    "build_report",
]

"""Schema v4: per-row ``account`` column on plans + orders.

A single canonical ledger file can host plans + orders from both Alpaca
paper accounts ('main' + 'test') simultaneously without collision. The
reconciler / submitter / exit-manager MUST filter by ``account`` so
cross-account UUID lookups never happen.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from alphalens_pipeline.paper.ledger import (
    LEDGER_SCHEMA_VERSION,
    VALID_ACCOUNTS,
    VALID_PLATFORMS,
    fetch_open_orders,
    fetch_orders_for_plan,
    fetch_plans_for_date,
    insert_fill,
    insert_order,
    insert_planned,
    open_ledger,
)


def _seed_plan(conn, *, brief_date: dt.date, ticker: str, account: str) -> int:
    plan_row = insert_planned(
        conn,
        brief_date=brief_date,
        ticker=ticker,
        theme="theme-x",
        planned_at=dt.datetime.now(dt.UTC),
        suggested_size_pct=1.0,
        scale_factor=1.0,
        final_size_pct=1.0,
        paper_equity=1_000_000.0,
        total_notional=1000.0,
        gross_notional=1000.0,
        disaster_stop=95.0,
        order_ttl_days=10,
        tiers=[(0, 100.0, 10, 100.0, "shallow")],
        tp_tranches=[(0, 110.0, 100.0, 1.0, "tp1")],
        account=account,
    )
    return plan_row.plan_id


class TestSchemaVersion(unittest.TestCase):
    def test_schema_version_is_5(self):
        self.assertEqual(LEDGER_SCHEMA_VERSION, 5)

    def test_valid_accounts_enum(self):
        self.assertEqual(VALID_ACCOUNTS, frozenset({"main", "test"}))

    def test_valid_platforms_enum(self):
        self.assertEqual(VALID_PLATFORMS, frozenset({"alpaca"}))


class TestPlansAccountColumn(unittest.TestCase):
    def test_same_ticker_same_date_different_accounts_coexist(self):
        """``UNIQUE(brief_date, ticker, account)`` allows the same ticker
        on the same date to have one plan per account — useful when both
        accounts are operating in parallel."""
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.db"
            with open_ledger(ledger) as conn:
                _seed_plan(conn, brief_date=dt.date(2026, 5, 27), ticker="AAPL", account="main")
                _seed_plan(conn, brief_date=dt.date(2026, 5, 27), ticker="AAPL", account="test")

                plans_main = fetch_plans_for_date(conn, dt.date(2026, 5, 27), account="main")
                plans_test = fetch_plans_for_date(conn, dt.date(2026, 5, 27), account="test")
                plans_all = fetch_plans_for_date(conn, dt.date(2026, 5, 27))

                self.assertEqual(len(plans_main), 1)
                self.assertEqual(plans_main[0]["account"], "main")
                self.assertEqual(len(plans_test), 1)
                self.assertEqual(plans_test[0]["account"], "test")
                self.assertEqual(len(plans_all), 2)

    def test_insert_planned_rejects_unknown_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.db"
            with open_ledger(ledger) as conn, self.assertRaises(ValueError):
                _seed_plan(
                    conn,
                    brief_date=dt.date(2026, 5, 27),
                    ticker="X",
                    account="staging",  # unknown
                )

    def test_fetch_plans_for_date_rejects_unknown_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.db"
            with open_ledger(ledger) as conn, self.assertRaises(ValueError):
                fetch_plans_for_date(conn, dt.date(2026, 5, 27), account="staging")


class TestOrdersAccountFiltering(unittest.TestCase):
    """The reconciler MUST filter by account — cross-account UUID lookups
    would 404. This pins that ``fetch_open_orders`` honours the filter."""

    def test_open_orders_scoped_to_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.db"
            with open_ledger(ledger) as conn:
                main_id = _seed_plan(
                    conn, brief_date=dt.date(2026, 5, 27), ticker="AAPL", account="main"
                )
                test_id = _seed_plan(
                    conn, brief_date=dt.date(2026, 5, 27), ticker="AAPL", account="test"
                )
                insert_order(
                    conn,
                    plan_id=main_id,
                    alpaca_order_id="uuid-main-001",
                    side="BUY",
                    order_kind="ENTRY",
                    order_type="LIMIT",
                    qty=10,
                    time_in_force="gtc",
                    submitted_at=dt.datetime.now(dt.UTC),
                    tier_index=0,
                    limit_price=100.0,
                    account="main",
                )
                insert_order(
                    conn,
                    plan_id=test_id,
                    alpaca_order_id="uuid-test-001",
                    side="BUY",
                    order_kind="ENTRY",
                    order_type="LIMIT",
                    qty=10,
                    time_in_force="gtc",
                    submitted_at=dt.datetime.now(dt.UTC),
                    tier_index=0,
                    limit_price=100.0,
                    account="test",
                )

                main_open = fetch_open_orders(conn, account="main")
                test_open = fetch_open_orders(conn, account="test")
                all_open = fetch_open_orders(conn)

                self.assertEqual(len(main_open), 1)
                self.assertEqual(main_open[0]["account"], "main")
                self.assertEqual(main_open[0]["alpaca_order_id"], "uuid-main-001")
                self.assertEqual(len(test_open), 1)
                self.assertEqual(test_open[0]["account"], "test")
                self.assertEqual(test_open[0]["alpaca_order_id"], "uuid-test-001")
                self.assertEqual(len(all_open), 2)

    def test_insert_order_rejects_unknown_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.db"
            with open_ledger(ledger) as conn:
                plan_id = _seed_plan(
                    conn, brief_date=dt.date(2026, 5, 27), ticker="X", account="main"
                )
                with self.assertRaises(ValueError):
                    insert_order(
                        conn,
                        plan_id=plan_id,
                        alpaca_order_id="uuid-bad",
                        side="BUY",
                        order_kind="ENTRY",
                        order_type="LIMIT",
                        qty=10,
                        time_in_force="gtc",
                        submitted_at=dt.datetime.now(dt.UTC),
                        account="staging",  # unknown
                    )

    def test_fetch_open_orders_rejects_unknown_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.db"
            with open_ledger(ledger) as conn, self.assertRaises(ValueError):
                fetch_open_orders(conn, account="staging")


class TestExitManagerThreadsAccountFromSnapshot(unittest.TestCase):
    """Per zen review (PR #279 INFO): pin the end-to-end invariant that
    a plan tagged ``account='test'`` produces exit orders (SL + TPs) all
    tagged ``account='test'``. Anchored at the snapshot boundary —
    ``_PlanSnapshot.account`` reads ``plans.account`` and threads through
    all 3 ``insert_order`` calls in :mod:`alphalens_pipeline.paper.exit_manager`.
    """

    def test_attach_exits_inherits_test_account_tag(self):
        from dataclasses import dataclass

        from alphalens_pipeline.paper.exit_manager import process_plan_exit

        @dataclass
        class _StubOrder:
            id: str

        class _StubClient:
            def __init__(self) -> None:
                self._next = 1

            def _emit(self) -> _StubOrder:
                oid = f"exit-{self._next:03d}"
                self._next += 1
                return _StubOrder(id=oid)

            def submit_stop_order(self, **kwargs):
                return self._emit()

            def submit_limit_order(self, **kwargs):
                return self._emit()

            def submit_market_order(self, **kwargs):
                return self._emit()

            def cancel_order(self, _id):
                return None

            def get_position(self, _symbol):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.db"
            with open_ledger(ledger) as conn:
                plan_id = _seed_plan(
                    conn,
                    brief_date=dt.date(2026, 5, 28),
                    ticker="NVDA",
                    account="test",
                )
                # Add a FILLED ENTRY so the snapshot enters the exit-attach
                # branch: entry_phase_settled + total_entry_filled_qty > 0.
                entry_id = insert_order(
                    conn,
                    plan_id=plan_id,
                    alpaca_order_id="entry-test-001",
                    side="BUY",
                    order_kind="ENTRY",
                    order_type="LIMIT",
                    qty=10,
                    time_in_force="gtc",
                    submitted_at=dt.datetime.now(dt.UTC),
                    tier_index=0,
                    limit_price=100.0,
                    status="FILLED",
                    account="test",
                )
                insert_fill(
                    conn,
                    order_id=entry_id,
                    alpaca_fill_id="entry-test-001-fill",
                    qty=10,
                    price=100.0,
                    filled_at=dt.datetime.now(dt.UTC),
                )

                outcome = process_plan_exit(conn, plan_id=plan_id, broker=_StubClient())

                self.assertEqual(outcome.action, "ATTACHED")
                self.assertGreater(outcome.n_exits_submitted, 0)

                # All non-entry orders (SL + TP) for this plan must be
                # tagged account='test'. The exit-attach branch routes
                # through _attach_exits which calls insert_order with
                # account=snapshot.account.
                orders = fetch_orders_for_plan(conn, plan_id)
                exit_orders = [o for o in orders if o["order_kind"] in ("SL", "TP")]
                self.assertGreater(len(exit_orders), 0)
                for o in exit_orders:
                    self.assertEqual(
                        o["account"],
                        "test",
                        f"exit order kind={o['order_kind']} leaked account="
                        f"{o['account']!r}, expected 'test'",
                    )


class TestDefaultAccountIsMain(unittest.TestCase):
    """Default for backward compatibility (in-code, not on-disk): callers
    that omit ``account=`` get 'main'. This pins the safe default so a
    fresh test forgetting the flag tags rows correctly."""

    def test_insert_planned_default_account_is_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.db"
            with open_ledger(ledger) as conn:
                # No account= argument — defaults to 'main'.
                insert_planned(
                    conn,
                    brief_date=dt.date(2026, 5, 27),
                    ticker="AAPL",
                    theme="t",
                    planned_at=dt.datetime.now(dt.UTC),
                    suggested_size_pct=1.0,
                    scale_factor=1.0,
                    final_size_pct=1.0,
                    paper_equity=1_000_000.0,
                    total_notional=1000.0,
                    gross_notional=1000.0,
                    disaster_stop=95.0,
                    order_ttl_days=10,
                    tiers=[(0, 100.0, 10, 100.0, "shallow")],
                    tp_tranches=[(0, 110.0, 100.0, 1.0, "tp1")],
                )
                plans = fetch_plans_for_date(conn, dt.date(2026, 5, 27))
                self.assertEqual(plans[0]["account"], "main")


class TestPlatformColumn(unittest.TestCase):
    """Schema v5 adds a per-row ``platform`` column on plans + orders
    (issue #388) — a separate axis from ``account``. Today only 'alpaca'
    is valid; the column exists so a second broker (e.g. IBKR) lands as
    data, not a migration."""

    def test_plan_platform_defaults_to_alpaca(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.db"
            with open_ledger(ledger) as conn:
                # _seed_plan calls insert_planned with NO platform arg.
                plan_id = _seed_plan(
                    conn, brief_date=dt.date(2026, 5, 27), ticker="AAPL", account="main"
                )
                row = conn.execute(
                    "SELECT platform FROM plans WHERE plan_id = ?", (plan_id,)
                ).fetchone()
                self.assertEqual(row["platform"], "alpaca")

    def test_invalid_platform_rejected_on_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.db"
            with open_ledger(ledger) as conn, self.assertRaises(ValueError):
                insert_planned(
                    conn,
                    brief_date=dt.date(2026, 5, 27),
                    ticker="AAPL",
                    theme="theme-x",
                    planned_at=dt.datetime.now(dt.UTC),
                    suggested_size_pct=1.0,
                    scale_factor=1.0,
                    final_size_pct=1.0,
                    paper_equity=1_000_000.0,
                    total_notional=1000.0,
                    gross_notional=1000.0,
                    disaster_stop=95.0,
                    order_ttl_days=10,
                    tiers=[(0, 100.0, 10, 100.0, "shallow")],
                    tp_tranches=[(0, 110.0, 100.0, 1.0, "tp1")],
                    account="main",
                    platform="ibkr",  # unknown
                )

    def test_invalid_platform_rejected_on_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.db"
            with open_ledger(ledger) as conn:
                plan_id = _seed_plan(
                    conn, brief_date=dt.date(2026, 5, 27), ticker="X", account="main"
                )
                with self.assertRaises(ValueError):
                    insert_order(
                        conn,
                        plan_id=plan_id,
                        alpaca_order_id="uuid-bad-platform",
                        side="BUY",
                        order_kind="ENTRY",
                        order_type="LIMIT",
                        qty=10,
                        time_in_force="gtc",
                        submitted_at=dt.datetime.now(dt.UTC),
                        tier_index=0,
                        limit_price=100.0,
                        account="main",
                        platform="ibkr",  # unknown
                    )

    def test_order_platform_defaults_to_alpaca(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.db"
            with open_ledger(ledger) as conn:
                plan_id = _seed_plan(
                    conn, brief_date=dt.date(2026, 5, 27), ticker="AAPL", account="main"
                )
                order_id = insert_order(
                    conn,
                    plan_id=plan_id,
                    alpaca_order_id="uuid-default-platform",
                    side="BUY",
                    order_kind="ENTRY",
                    order_type="LIMIT",
                    qty=10,
                    time_in_force="gtc",
                    submitted_at=dt.datetime.now(dt.UTC),
                    tier_index=0,
                    limit_price=100.0,
                    account="main",
                    # No platform= argument — defaults to 'alpaca'.
                )
                row = conn.execute(
                    "SELECT platform FROM orders WHERE order_id = ?", (order_id,)
                ).fetchone()
                self.assertEqual(row["platform"], "alpaca")

    def test_plans_unique_includes_platform(self):
        # The plans UNIQUE constraint widened to include `platform` in v5.
        # A real 2-platform coexistence row (same brief_date/ticker/account,
        # different platform) is impossible to insert while
        # VALID_PLATFORMS == {'alpaca'} — the CHECK(platform IN ('alpaca'))
        # rejects any second value — so introspecting the DDL is the honest
        # pin that the uniqueness axis was widened.
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.db"
            with open_ledger(ledger) as conn:
                ddl = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='plans'"
                ).fetchone()["sql"]
                self.assertIn("UNIQUE(brief_date, ticker, account, platform)", ddl)


class TestRunbookAlterMigration(unittest.TestCase):
    """Pins the EXACT ``ALTER TABLE`` the operator runs on the live VPS
    ledger to migrate an existing v4 file (account-only schema) up to v5.

    v5 is a runbook-only migration (no in-code migrate step) per the v4
    precedent. SQLite ``ALTER TABLE ... ADD COLUMN`` can add the column
    and backfill the DEFAULT into pre-existing rows, but it CANNOT widen
    the existing UNIQUE constraint — the migrated table keeps the OLD
    3-col ``UNIQUE(brief_date, ticker, account)``. That is the documented
    deferral: a full table rebuild would be needed to widen it, which is
    unnecessary while only one platform exists.
    """

    _OLD_V4_PLANS_DDL = """
        CREATE TABLE plans (
            plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
            brief_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            account TEXT NOT NULL DEFAULT 'main' CHECK(account IN ('main', 'test')),
            UNIQUE(brief_date, ticker, account)
        )
    """

    _RUNBOOK_ALTER = "ALTER TABLE plans ADD COLUMN platform TEXT NOT NULL DEFAULT 'alpaca'"

    def test_runbook_alter_backfills_platform_and_keeps_old_unique(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "v4_ledger.db"
            conn = sqlite3.connect(ledger)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute(self._OLD_V4_PLANS_DDL)
                conn.execute(
                    "INSERT INTO plans (brief_date, ticker, account) VALUES (?, ?, ?)",
                    ("2026-05-27", "AAPL", "main"),
                )
                conn.commit()

                # Operator runbook migration.
                conn.execute(self._RUNBOOK_ALTER)
                conn.commit()

                # (a) the column now exists.
                cols = {r["name"] for r in conn.execute("PRAGMA table_info(plans)")}
                self.assertIn("platform", cols)

                # (b) the pre-existing row's platform backfilled to 'alpaca'.
                row = conn.execute(
                    "SELECT platform FROM plans WHERE ticker = ?", ("AAPL",)
                ).fetchone()
                self.assertEqual(row["platform"], "alpaca")

                # (c) the table still carries the OLD 3-col UNIQUE — ALTER
                #     cannot widen it (documented deferral).
                ddl = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='plans'"
                ).fetchone()["sql"]
                self.assertIn("UNIQUE(brief_date, ticker, account)", ddl)
                self.assertNotIn("UNIQUE(brief_date, ticker, account, platform)", ddl)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

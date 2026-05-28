"""SQLite ledger tests — schema creation, idempotency, insert/query.

Uses ``tmp_path`` to keep each test in an isolated DB file. The schema is
declared idempotently via ``CREATE TABLE IF NOT EXISTS`` so the same DB
can be opened twice without conflict — the second test pins that property.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import unittest

from alphalens_pipeline.paper.ledger import (
    LEDGER_SCHEMA_VERSION,
    count_plans_for_date,
    count_shadow_for_date,
    fetch_plans_for_date,
    fetch_shadow_for_date,
    init_ledger,
    insert_planned,
    insert_shadow,
    open_ledger,
)


def _tmpdb(tmp_dir):
    """Return a path to a per-test SQLite file. Caller may delete or reuse."""
    return tmp_dir / "ledger.db"


class TestSchema(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = _tmpdb(__import__("pathlib").Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_init_creates_expected_tables(self):
        init_ledger(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            tables = {row[0] for row in cur.fetchall()}
        self.assertIn("plans", tables)
        self.assertIn("plan_entries", tables)
        self.assertIn("plan_exits", tables)
        self.assertIn("shadow_log", tables)
        self.assertIn("meta", tables)

    def test_init_records_schema_version(self):
        init_ledger(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
            version = cur.fetchone()[0]
        self.assertEqual(version, str(LEDGER_SCHEMA_VERSION))

    def test_init_is_idempotent(self):
        """Two consecutive init calls must succeed; the second is a no-op."""
        init_ledger(self.db_path)
        init_ledger(self.db_path)  # must not raise

    def test_plans_uniqueness_per_date_ticker(self):
        init_ledger(self.db_path)
        d = dt.date(2026, 5, 28)
        ts = dt.datetime.now(dt.UTC)
        with open_ledger(self.db_path) as conn:
            insert_planned(
                conn,
                brief_date=d,
                ticker="NVDA",
                theme="ai-infra",
                planned_at=ts,
                suggested_size_pct=5.0,
                scale_factor=0.0556,
                final_size_pct=0.278,
                paper_equity=1_000_000.0,
                total_notional=2778.0,
                gross_notional=2700.0,
                disaster_stop=80.0,
                order_ttl_days=10,
                tiers=[(0, 100.0, 10, 50.0, "t0")],
                tp_tranches=[(0, 110.0, 100.0, 1.0, "tp")],
            )
            with self.assertRaises(sqlite3.IntegrityError):
                insert_planned(
                    conn,
                    brief_date=d,
                    ticker="NVDA",
                    theme="ai-infra",
                    planned_at=ts,
                    suggested_size_pct=5.0,
                    scale_factor=0.0556,
                    final_size_pct=0.278,
                    paper_equity=1_000_000.0,
                    total_notional=2778.0,
                    gross_notional=2700.0,
                    disaster_stop=80.0,
                    order_ttl_days=10,
                    tiers=[(0, 100.0, 10, 50.0, "t0")],
                    tp_tranches=[(0, 110.0, 100.0, 1.0, "tp")],
                )


class TestInsertPlanned(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = _tmpdb(__import__("pathlib").Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_insert_writes_one_plan_plus_tiers_plus_tps(self):
        d = dt.date(2026, 5, 28)
        ts = dt.datetime.now(dt.UTC)
        with open_ledger(self.db_path) as conn:
            row = insert_planned(
                conn,
                brief_date=d,
                ticker="NVDA",
                theme="ai-infra",
                planned_at=ts,
                suggested_size_pct=5.0,
                scale_factor=0.0556,
                final_size_pct=0.278,
                paper_equity=1_000_000.0,
                total_notional=2778.0,
                gross_notional=2700.0,
                disaster_stop=80.0,
                order_ttl_days=10,
                tiers=[
                    (0, 100.0, 13, 50.0, "t0"),
                    (1, 95.0, 8, 30.0, "t1"),
                    (2, 90.0, 6, 20.0, "t2"),
                ],
                tp_tranches=[
                    (0, 110.0, 50.0, 1.0, "tp1"),
                    (1, 120.0, 50.0, 2.0, "tp2"),
                ],
            )

            self.assertEqual(row.status, "PLANNED")
            self.assertEqual(row.ticker, "NVDA")

            n_tiers = conn.execute(
                "SELECT COUNT(*) FROM plan_entries WHERE plan_id = ?", (row.plan_id,)
            ).fetchone()[0]
            self.assertEqual(n_tiers, 3)
            n_tps = conn.execute(
                "SELECT COUNT(*) FROM plan_exits WHERE plan_id = ?", (row.plan_id,)
            ).fetchone()[0]
            self.assertEqual(n_tps, 2)

    def test_cascade_delete_drops_tiers_and_tps(self):
        d = dt.date(2026, 5, 28)
        ts = dt.datetime.now(dt.UTC)
        with open_ledger(self.db_path) as conn:
            row = insert_planned(
                conn,
                brief_date=d,
                ticker="NVDA",
                theme="ai-infra",
                planned_at=ts,
                suggested_size_pct=5.0,
                scale_factor=0.0556,
                final_size_pct=0.278,
                paper_equity=1_000_000.0,
                total_notional=2778.0,
                gross_notional=2700.0,
                disaster_stop=80.0,
                order_ttl_days=10,
                tiers=[(0, 100.0, 13, 50.0, "t0")],
                tp_tranches=[(0, 110.0, 100.0, 1.0, "tp")],
            )

            conn.execute("DELETE FROM plans WHERE plan_id = ?", (row.plan_id,))
            n_tiers = conn.execute(
                "SELECT COUNT(*) FROM plan_entries WHERE plan_id = ?", (row.plan_id,)
            ).fetchone()[0]
            self.assertEqual(n_tiers, 0)


class TestShadowLog(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = _tmpdb(__import__("pathlib").Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_insert_shadow_returns_id_and_persists(self):
        d = dt.date(2026, 5, 28)
        with open_ledger(self.db_path) as conn:
            log_id = insert_shadow(
                conn,
                brief_date=d,
                ticker="NVDA",
                theme="ai-infra",
                reason="same_ticker_open",
                details={"hint": "already long NVDA from 2026-05-22"},
            )
            self.assertIsInstance(log_id, int)
            self.assertGreater(log_id, 0)

            rows = fetch_shadow_for_date(conn, d)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["reason"], "same_ticker_open")
            self.assertIn("already long", rows[0]["details_json"])

    def test_insert_shadow_with_no_details_writes_empty_object(self):
        d = dt.date(2026, 5, 28)
        with open_ledger(self.db_path) as conn:
            insert_shadow(conn, brief_date=d, ticker="X", theme="t", reason="not_verified")
            rows = fetch_shadow_for_date(conn, d)
            self.assertEqual(rows[0]["details_json"], "{}")


class TestCountQueries(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = _tmpdb(__import__("pathlib").Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_counts_and_fetch_for_specific_date(self):
        d = dt.date(2026, 5, 28)
        other = dt.date(2026, 5, 27)
        ts = dt.datetime.now(dt.UTC)
        with open_ledger(self.db_path) as conn:
            insert_planned(
                conn,
                brief_date=d,
                ticker="NVDA",
                theme="ai",
                planned_at=ts,
                suggested_size_pct=5.0,
                scale_factor=0.0556,
                final_size_pct=0.278,
                paper_equity=1_000_000.0,
                total_notional=2778.0,
                gross_notional=2700.0,
                disaster_stop=80.0,
                order_ttl_days=10,
                tiers=[(0, 100.0, 13, 50.0, "t0")],
                tp_tranches=[],
            )
            insert_planned(
                conn,
                brief_date=other,
                ticker="AVGO",
                theme="ai",
                planned_at=ts,
                suggested_size_pct=5.0,
                scale_factor=0.0556,
                final_size_pct=0.278,
                paper_equity=1_000_000.0,
                total_notional=2778.0,
                gross_notional=2700.0,
                disaster_stop=80.0,
                order_ttl_days=10,
                tiers=[(0, 100.0, 13, 50.0, "t0")],
                tp_tranches=[],
            )
            insert_shadow(conn, brief_date=d, ticker="A", theme="t", reason="not_verified")

            self.assertEqual(count_plans_for_date(conn, d), 1)
            self.assertEqual(count_plans_for_date(conn, other), 1)
            self.assertEqual(count_shadow_for_date(conn, d), 1)
            self.assertEqual(count_shadow_for_date(conn, other), 0)

            plans_for_d = fetch_plans_for_date(conn, d)
            self.assertEqual(len(plans_for_d), 1)
            self.assertEqual(plans_for_d[0]["ticker"], "NVDA")


class TestParentDirAutoCreate(unittest.TestCase):
    def test_init_creates_parent_directory(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "nonexistent_subdir" / "ledger.db"
            init_ledger(nested)  # must not raise
            self.assertTrue(nested.exists())


# ----- v3: orders / fills / plan_outcomes -----


class _LedgerWithPlan(unittest.TestCase):
    """Per-test setup that creates a single PLANNED row so order/fill tests
    have a parent ``plan_id`` to reference. The plan itself is the same
    payload across tests — only the per-test order / fill machinery
    differs."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = _tmpdb(__import__("pathlib").Path(self._tmp.name))
        self.ts = dt.datetime.now(dt.UTC)
        self.d = dt.date(2026, 5, 28)
        with open_ledger(self.db_path) as conn:
            row = insert_planned(
                conn,
                brief_date=self.d,
                ticker="NVDA",
                theme="ai-infra",
                planned_at=self.ts,
                suggested_size_pct=5.0,
                scale_factor=0.0556,
                final_size_pct=0.278,
                paper_equity=1_000_000.0,
                total_notional=2778.0,
                gross_notional=2700.0,
                disaster_stop=80.0,
                order_ttl_days=10,
                tiers=[(0, 100.0, 27, 100.0, "t0")],
                tp_tranches=[(0, 120.0, 100.0, 2.0, "tp")],
            )
            self.plan_id = row.plan_id

    def tearDown(self):
        self._tmp.cleanup()


class TestOrdersTable(_LedgerWithPlan):
    def test_insert_order_persists_all_fields(self):
        from alphalens_pipeline.paper.ledger import (
            fetch_orders_for_plan,
            insert_order,
        )

        with open_ledger(self.db_path) as conn:
            oid = insert_order(
                conn,
                plan_id=self.plan_id,
                alpaca_order_id="abc-123",
                side="BUY",
                order_kind="ENTRY",
                tier_index=0,
                order_type="LIMIT",
                qty=27,
                limit_price=100.0,
                time_in_force="gtc",
                submitted_at=self.ts,
            )
            self.assertGreater(oid, 0)
            rows = fetch_orders_for_plan(conn, self.plan_id)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["alpaca_order_id"], "abc-123")
            self.assertEqual(row["side"], "BUY")
            self.assertEqual(row["order_kind"], "ENTRY")
            self.assertEqual(row["qty"], 27)
            self.assertEqual(row["status"], "SUBMITTED")

    def test_alpaca_order_id_uniqueness_blocks_double_insert(self):
        """Re-running the submitter on a previously-submitted plan must not
        create a second orders row — the alpaca_order_id UNIQUE constraint
        protects against idempotency violations from a crash-then-rerun."""
        from alphalens_pipeline.paper.ledger import insert_order

        with open_ledger(self.db_path) as conn:
            insert_order(
                conn,
                plan_id=self.plan_id,
                alpaca_order_id="abc-123",
                side="BUY",
                order_kind="ENTRY",
                order_type="LIMIT",
                qty=27,
                limit_price=100.0,
                time_in_force="gtc",
                submitted_at=self.ts,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                insert_order(
                    conn,
                    plan_id=self.plan_id,
                    alpaca_order_id="abc-123",
                    side="BUY",
                    order_kind="ENTRY",
                    order_type="LIMIT",
                    qty=27,
                    limit_price=100.0,
                    time_in_force="gtc",
                    submitted_at=self.ts,
                )

    def test_update_order_status_moves_through_lifecycle(self):
        from alphalens_pipeline.paper.ledger import (
            fetch_orders_for_plan,
            insert_order,
            update_order_status,
        )

        with open_ledger(self.db_path) as conn:
            oid = insert_order(
                conn,
                plan_id=self.plan_id,
                alpaca_order_id="abc-123",
                side="BUY",
                order_kind="ENTRY",
                order_type="LIMIT",
                qty=27,
                limit_price=100.0,
                time_in_force="gtc",
                submitted_at=self.ts,
            )
            update_order_status(conn, order_id=oid, status="PARTIALLY_FILLED")
            update_order_status(conn, order_id=oid, status="FILLED")
            row = fetch_orders_for_plan(conn, self.plan_id)[0]
            self.assertEqual(row["status"], "FILLED")

    def test_fetch_open_orders_returns_only_non_terminal(self):
        from alphalens_pipeline.paper.ledger import (
            fetch_open_orders,
            insert_order,
        )

        with open_ledger(self.db_path) as conn:
            o1 = insert_order(
                conn,
                plan_id=self.plan_id,
                alpaca_order_id="open-1",
                side="BUY",
                order_kind="ENTRY",
                order_type="LIMIT",
                qty=10,
                limit_price=100.0,
                time_in_force="gtc",
                submitted_at=self.ts,
            )
            insert_order(
                conn,
                plan_id=self.plan_id,
                alpaca_order_id="closed-1",
                side="BUY",
                order_kind="ENTRY",
                order_type="LIMIT",
                qty=10,
                limit_price=100.0,
                time_in_force="gtc",
                submitted_at=self.ts,
                status="FILLED",
            )
            insert_order(
                conn,
                plan_id=self.plan_id,
                alpaca_order_id="canceled-1",
                side="BUY",
                order_kind="ENTRY",
                order_type="LIMIT",
                qty=10,
                limit_price=100.0,
                time_in_force="gtc",
                submitted_at=self.ts,
                status="CANCELED",
            )
            open_rows = fetch_open_orders(conn)
            self.assertEqual({r["order_id"] for r in open_rows}, {o1})


class TestFillsTable(_LedgerWithPlan):
    def test_insert_fill_persists_and_retrieves(self):
        from alphalens_pipeline.paper.ledger import (
            fetch_fills_for_order,
            insert_fill,
            insert_order,
        )

        with open_ledger(self.db_path) as conn:
            oid = insert_order(
                conn,
                plan_id=self.plan_id,
                alpaca_order_id="abc-123",
                side="BUY",
                order_kind="ENTRY",
                order_type="LIMIT",
                qty=27,
                limit_price=100.0,
                time_in_force="gtc",
                submitted_at=self.ts,
            )
            insert_fill(
                conn,
                order_id=oid,
                alpaca_fill_id="fill-1",
                qty=10,
                price=99.5,
                filled_at=self.ts,
            )
            insert_fill(
                conn,
                order_id=oid,
                alpaca_fill_id="fill-2",
                qty=17,
                price=99.7,
                filled_at=self.ts,
            )
            fills = fetch_fills_for_order(conn, oid)
            self.assertEqual(len(fills), 2)
            self.assertEqual(sum(f["qty"] for f in fills), 27)

    def test_alpaca_fill_id_dedup_blocks_double_insert(self):
        """Reconciler idempotency: re-running on the same Alpaca activity
        stream must not create duplicate fill rows."""
        from alphalens_pipeline.paper.ledger import insert_fill, insert_order

        with open_ledger(self.db_path) as conn:
            oid = insert_order(
                conn,
                plan_id=self.plan_id,
                alpaca_order_id="abc-123",
                side="BUY",
                order_kind="ENTRY",
                order_type="LIMIT",
                qty=27,
                limit_price=100.0,
                time_in_force="gtc",
                submitted_at=self.ts,
            )
            insert_fill(
                conn,
                order_id=oid,
                alpaca_fill_id="fill-1",
                qty=10,
                price=99.5,
                filled_at=self.ts,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                insert_fill(
                    conn,
                    order_id=oid,
                    alpaca_fill_id="fill-1",
                    qty=10,
                    price=99.5,
                    filled_at=self.ts,
                )

    def test_fills_cascade_delete_when_order_dropped(self):
        from alphalens_pipeline.paper.ledger import (
            fetch_fills_for_order,
            insert_fill,
            insert_order,
        )

        with open_ledger(self.db_path) as conn:
            oid = insert_order(
                conn,
                plan_id=self.plan_id,
                alpaca_order_id="abc-123",
                side="BUY",
                order_kind="ENTRY",
                order_type="LIMIT",
                qty=27,
                limit_price=100.0,
                time_in_force="gtc",
                submitted_at=self.ts,
            )
            insert_fill(
                conn,
                order_id=oid,
                alpaca_fill_id="fill-1",
                qty=10,
                price=99.5,
                filled_at=self.ts,
            )
            conn.execute("DELETE FROM orders WHERE order_id = ?", (oid,))
            self.assertEqual(len(fetch_fills_for_order(conn, oid)), 0)


class TestPlanOutcomes(_LedgerWithPlan):
    def test_insert_plan_outcome_succeeds(self):
        from alphalens_pipeline.paper.ledger import insert_plan_outcome

        with open_ledger(self.db_path) as conn:
            oid = insert_plan_outcome(
                conn,
                plan_id=self.plan_id,
                exit_kind="TP_HIT",
                first_fill_at=self.ts,
                last_exit_at=self.ts,
                blended_entry_price=100.0,
                blended_exit_price=120.0,
                realized_r_multiple=2.0,
                closed_at=self.ts,
            )
            self.assertGreater(oid, 0)

    def test_plan_outcome_uniqueness_per_plan(self):
        """One plan, one outcome. A second insert against the same plan_id
        must fail; the reconciler should look up + update the existing row
        if the outcome needs revision."""
        from alphalens_pipeline.paper.ledger import insert_plan_outcome

        with open_ledger(self.db_path) as conn:
            insert_plan_outcome(
                conn,
                plan_id=self.plan_id,
                exit_kind="TP_HIT",
                closed_at=self.ts,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                insert_plan_outcome(
                    conn,
                    plan_id=self.plan_id,
                    exit_kind="SL_HIT",
                    closed_at=self.ts,
                )

    def test_cascade_delete_plan_drops_outcome(self):
        from alphalens_pipeline.paper.ledger import insert_plan_outcome

        with open_ledger(self.db_path) as conn:
            insert_plan_outcome(
                conn,
                plan_id=self.plan_id,
                exit_kind="TP_HIT",
                closed_at=self.ts,
            )
            conn.execute("DELETE FROM plans WHERE plan_id = ?", (self.plan_id,))
            n = conn.execute(
                "SELECT COUNT(*) FROM plan_outcomes WHERE plan_id = ?", (self.plan_id,)
            ).fetchone()[0]
            self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()

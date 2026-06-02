"""Schema v6: OCO exit-group correlation id + ExitLadderLeg persistence.

PR-2 of the OCO-ladder track. The ledger gains a nullable
``orders.exit_group_id`` column that links the take-profit and stop-loss
legs of ONE OCO group, plus ``record_exit_ladder`` to persist a
broker-neutral attached exit-ladder (a list of
:class:`alphalens_pipeline.paper.broker.ExitLadderLeg`). NO reconciler /
exit_manager / planner behaviour changes here — this PR only lets the
ledger STORE an attached OCO-ladder.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path

from alphalens_pipeline.paper.broker import ExitLadderLeg
from alphalens_pipeline.paper.ledger import (
    LEDGER_SCHEMA_VERSION,
    fetch_orders_for_plan,
    init_ledger,
    insert_order,
    insert_planned,
    open_ledger,
    record_exit_ladder,
)


def _seed_plan(conn, *, brief_date: dt.date, ticker: str) -> int:
    """Insert one PLANNED parent row so the orders FK resolves."""
    row = insert_planned(
        conn,
        brief_date=brief_date,
        ticker=ticker,
        theme="ai-infra",
        planned_at=dt.datetime.now(dt.UTC),
        suggested_size_pct=5.0,
        scale_factor=1.0,
        final_size_pct=5.0,
        paper_equity=1_000_000.0,
        total_notional=50_000.0,
        gross_notional=50_000.0,
        disaster_stop=80.0,
        order_ttl_days=10,
        tiers=[(0, 100.0, 100, 100.0, "t0")],
        tp_tranches=[(0, 120.0, 100.0, 2.0, "tp")],
    )
    return row.plan_id


class TestSchemaV6(unittest.TestCase):
    """Schema version bump + the new exit_group_id column."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "ledger.db"

    def tearDown(self):
        self._tmp.cleanup()

    def test_given_fresh_ledger_when_initialised_then_schema_version_is_6(self):
        # Given / When
        init_ledger(self.db_path)
        # Then
        self.assertEqual(LEDGER_SCHEMA_VERSION, 6)
        with sqlite3.connect(self.db_path) as conn:
            version = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        self.assertEqual(version, "6")

    def test_given_fresh_ledger_when_initialised_then_orders_has_exit_group_id(self):
        # Given / When
        init_ledger(self.db_path)
        # Then
        with sqlite3.connect(self.db_path) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
        self.assertIn("exit_group_id", cols)


class TestV6SchemaGuard(unittest.TestCase):
    """Pins the deploy-before-ALTER fail-fast for the v6 exit_group_id column.

    Mirrors the v5 platform-column guard: the column lands on an
    operator-migrated ledger via a runbook ``ALTER TABLE ... ADD COLUMN``,
    NOT in-code (``CREATE TABLE IF NOT EXISTS`` never widens an existing
    table). If new code is deployed BEFORE the operator runs that ALTER,
    inserts would emit ``exit_group_id`` into a table that lacks it and
    SQLite would raise a cryptic ``OperationalError`` mid-insert.
    ``init_ledger`` calls ``_assert_v6_exit_group_column`` after the
    idempotent CREATE loop so a stale pre-v6 file fails with a clear
    instruction instead.
    """

    _META_DDL = """
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """

    # Pre-v6 orders table: carries platform (so the v5 guard passes) +
    # status/order_kind (so the idempotent CREATE INDEX DDL resolves) but
    # NO exit_group_id (the whole point of this guard).
    _OLD_V5_ORDERS_DDL = """
        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL,
            alpaca_order_id TEXT NOT NULL UNIQUE,
            order_kind TEXT NOT NULL,
            status TEXT NOT NULL,
            account TEXT NOT NULL DEFAULT 'main',
            platform TEXT NOT NULL DEFAULT 'alpaca'
        )
    """

    _OLD_V5_PLANS_DDL = """
        CREATE TABLE plans (
            plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
            brief_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            account TEXT NOT NULL DEFAULT 'main',
            platform TEXT NOT NULL DEFAULT 'alpaca',
            UNIQUE(brief_date, ticker, account)
        )
    """

    def test_given_pre_v6_file_when_init_ledger_then_raises_with_runbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "v5_ledger.db"
            # Given: an operator-migrated v5 file WITHOUT exit_group_id.
            conn = sqlite3.connect(ledger)
            try:
                conn.execute(self._META_DDL)
                conn.execute(self._OLD_V5_PLANS_DDL)
                conn.execute(self._OLD_V5_ORDERS_DDL)
                conn.commit()
            finally:
                conn.close()

            # When / Then
            with self.assertRaises(RuntimeError) as ctx:
                init_ledger(ledger)

            message = str(ctx.exception)
            self.assertIn("exit_group_id", message)
            self.assertIn("ALTER TABLE", message)

    def test_given_fresh_db_when_init_ledger_then_guard_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "fresh.db"
            # Given fresh DB / When / Then: no raise.
            init_ledger(ledger)
            init_ledger(ledger)  # idempotent re-run also passes the guard


class TestInsertOrderExitGroupId(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "ledger.db"
        self.ts = dt.datetime.now(dt.UTC)
        with open_ledger(self.db_path) as conn:
            self.plan_id = _seed_plan(conn, brief_date=dt.date(2026, 5, 28), ticker="NVDA")

    def tearDown(self):
        self._tmp.cleanup()

    def test_given_entry_order_when_inserted_then_exit_group_id_defaults_null(self):
        # Given / When: a normal ENTRY order with no exit_group_id.
        with open_ledger(self.db_path) as conn:
            insert_order(
                conn,
                plan_id=self.plan_id,
                alpaca_order_id="entry-1",
                side="BUY",
                order_kind="ENTRY",
                order_type="LIMIT",
                qty=100,
                limit_price=100.0,
                time_in_force="gtc",
                submitted_at=self.ts,
            )
            row = fetch_orders_for_plan(conn, self.plan_id)[0]
        # Then
        self.assertIsNone(row["exit_group_id"])

    def test_given_exit_group_id_when_inserted_then_persisted(self):
        # Given / When: an order that carries an exit_group_id.
        with open_ledger(self.db_path) as conn:
            insert_order(
                conn,
                plan_id=self.plan_id,
                alpaca_order_id="tp-1",
                side="SELL",
                order_kind="TP",
                order_type="LIMIT",
                qty=50,
                limit_price=120.0,
                time_in_force="gtc",
                submitted_at=self.ts,
                exit_group_id="tp-1",
            )
            row = fetch_orders_for_plan(conn, self.plan_id)[0]
        # Then
        self.assertEqual(row["exit_group_id"], "tp-1")


class TestRecordExitLadder(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "ledger.db"
        self.ts = dt.datetime.now(dt.UTC)
        with open_ledger(self.db_path) as conn:
            self.plan_id = _seed_plan(conn, brief_date=dt.date(2026, 5, 28), ticker="NVDA")

    def tearDown(self):
        self._tmp.cleanup()

    def _three_leg_ladder(self) -> list[ExitLadderLeg]:
        # Real ExitLadderLeg (PR-1 dataclass) so the attribute contract is
        # exercised end-to-end. One disaster stop (85.0) repeated across the
        # 3 tranches; each tranche owns its own tp/sl order ids.
        return [
            ExitLadderLeg(
                tranche_index=0,
                qty=40,
                take_profit_limit=110.0,
                stop_price=85.0,
                tp_order_id="tp-0",
                sl_order_id="sl-0",
            ),
            ExitLadderLeg(
                tranche_index=1,
                qty=35,
                take_profit_limit=120.0,
                stop_price=85.0,
                tp_order_id="tp-1",
                sl_order_id="sl-1",
            ),
            ExitLadderLeg(
                tranche_index=2,
                qty=25,
                take_profit_limit=130.0,
                stop_price=85.0,
                tp_order_id="tp-2",
                sl_order_id="sl-2",
            ),
        ]

    def test_given_three_leg_ladder_when_recorded_then_six_orders_rows(self):
        # Given a 3-leg ladder / When recorded
        with open_ledger(self.db_path) as conn:
            order_ids = record_exit_ladder(
                conn,
                plan_id=self.plan_id,
                legs=self._three_leg_ladder(),
                submitted_at=self.ts,
            )
            rows = fetch_orders_for_plan(conn, self.plan_id)
        # Then: 3 TP LIMIT + 3 SL STOP = 6 rows, 6 returned ids.
        self.assertEqual(len(order_ids), 6)
        self.assertEqual(len(rows), 6)
        tp_rows = [r for r in rows if r["order_kind"] == "TP"]
        sl_rows = [r for r in rows if r["order_kind"] == "SL"]
        self.assertEqual(len(tp_rows), 3)
        self.assertEqual(len(sl_rows), 3)
        self.assertTrue(all(r["order_type"] == "LIMIT" for r in tp_rows))
        self.assertTrue(all(r["order_type"] == "STOP" for r in sl_rows))
        self.assertTrue(all(r["side"] == "SELL" for r in rows))

    def test_given_recorded_ladder_when_querying_by_group_then_pair_shares_id(self):
        # Given a recorded ladder
        with open_ledger(self.db_path) as conn:
            record_exit_ladder(
                conn,
                plan_id=self.plan_id,
                legs=self._three_leg_ladder(),
                submitted_at=self.ts,
            )
            # When: query the group id of the middle tranche (its tp_order_id).
            pair = conn.execute(
                "SELECT * FROM orders WHERE exit_group_id = ? ORDER BY order_kind",
                ("tp-1",),
            ).fetchall()
        # Then: exactly the TP + SL pair of that tranche.
        self.assertEqual(len(pair), 2)
        kinds = {r["order_kind"] for r in pair}
        self.assertEqual(kinds, {"TP", "SL"})
        self.assertTrue(all(r["exit_group_id"] == "tp-1" for r in pair))
        self.assertTrue(all(r["tranche_index"] == 1 for r in pair))

    def test_given_recorded_ladder_when_inspecting_then_prices_and_qty_correct(self):
        leg = self._three_leg_ladder()[0]
        with open_ledger(self.db_path) as conn:
            record_exit_ladder(
                conn,
                plan_id=self.plan_id,
                legs=[leg],
                submitted_at=self.ts,
            )
            rows = fetch_orders_for_plan(conn, self.plan_id)

        by_kind = {r["order_kind"]: r for r in rows}
        # TP row: LIMIT, take_profit_limit on limit_price, stop_price NULL.
        tp = by_kind["TP"]
        self.assertEqual(tp["limit_price"], leg.take_profit_limit)
        self.assertIsNone(tp["stop_price"])
        self.assertEqual(tp["qty"], leg.qty)
        self.assertEqual(tp["tranche_index"], leg.tranche_index)
        self.assertEqual(tp["exit_group_id"], leg.tp_order_id)
        self.assertEqual(tp["alpaca_order_id"], leg.tp_order_id)
        # SL row: STOP, stop_price set, limit_price NULL, grouped by tp id.
        sl = by_kind["SL"]
        self.assertEqual(sl["stop_price"], leg.stop_price)
        self.assertIsNone(sl["limit_price"])
        self.assertEqual(sl["qty"], leg.qty)
        self.assertEqual(sl["tranche_index"], leg.tranche_index)
        self.assertEqual(sl["exit_group_id"], leg.tp_order_id)
        self.assertEqual(sl["alpaca_order_id"], leg.sl_order_id)

    def test_given_empty_legs_when_recorded_then_raises_and_persists_nothing(self):
        # Given an empty ladder (a caller bug — no take-profit tranche means no
        # protective stop attaches, the "silently drop the stop" failure the
        # broker-side attach_exit_ladder guard also refuses).
        with open_ledger(self.db_path) as conn:
            # When / Then: the persistence helper refuses rather than no-op [].
            with self.assertRaises(ValueError):
                record_exit_ladder(
                    conn,
                    plan_id=self.plan_id,
                    legs=[],
                    submitted_at=self.ts,
                )
            # And: nothing was persisted (no half-written exit rows).
            rows = fetch_orders_for_plan(conn, self.plan_id)
        self.assertEqual([r for r in rows if r["order_kind"] in ("TP", "SL")], [])

    def test_given_test_account_when_recorded_then_both_legs_route_to_it(self):
        # Given a non-default account/platform threaded through record_exit_ladder.
        leg = self._three_leg_ladder()[0]
        with open_ledger(self.db_path) as conn:
            record_exit_ladder(
                conn,
                plan_id=self.plan_id,
                legs=[leg],
                submitted_at=self.ts,
                account="test",
            )
            rows = fetch_orders_for_plan(conn, self.plan_id)
        exit_rows = [r for r in rows if r["order_kind"] in ("TP", "SL")]
        self.assertEqual(len(exit_rows), 2)
        # Both the TP and SL leg carry the routed account + GTC time-in-force.
        self.assertTrue(all(r["account"] == "test" for r in exit_rows))
        self.assertTrue(all(r["time_in_force"] == "gtc" for r in exit_rows))


if __name__ == "__main__":
    unittest.main()

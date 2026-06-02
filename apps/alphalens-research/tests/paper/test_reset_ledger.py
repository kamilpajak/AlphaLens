"""Unit tests for ``reset_paper_chain`` (ledger.py).

The reset helper clears the OPEN paper-chain state (plans / orders /
fills / plan_entries / plan_exits) so the reconciler stops tracking the
orphaned chain, while KEEPING the ``plan_outcomes`` closed-position
history + the ``meta`` schema_version row + the per-row ``platform``
column intact.

Run via the research unittest discover harness (NOT pytest).
"""

from __future__ import annotations

import datetime as dt
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from alphalens_pipeline.paper.ledger import (
    LEDGER_SCHEMA_VERSION,
    insert_fill,
    insert_order,
    insert_plan_outcome,
    insert_planned,
    open_ledger,
    reset_paper_chain,
)

_NOW = dt.datetime(2026, 6, 1, 13, 30, tzinfo=dt.UTC)
_BRIEF_DATE = dt.date(2026, 5, 30)


def _seed_full_chain(
    conn,
    *,
    account: str = "test",
    ticker: str = "AAPL",
    order_uid: str = "alpaca-order-1",
    fill_uid: str = "alpaca-fill-1",
) -> int:
    """Insert one plan with a tier + a TP tranche + an order + a fill +
    a closed outcome. Returns the plan_id. ``account`` / unique-id args
    let one ledger hold a side-by-side ``main`` + ``test`` chain."""
    row = insert_planned(
        conn,
        brief_date=_BRIEF_DATE,
        ticker=ticker,
        theme="services",
        planned_at=_NOW,
        suggested_size_pct=1.0,
        scale_factor=1.0,
        final_size_pct=1.0,
        paper_equity=1_000_000.0,
        total_notional=10_000.0,
        gross_notional=10_000.0,
        disaster_stop=90.0,
        order_ttl_days=2,
        tiers=[(0, 100.0, 100, 100.0, "entry")],
        tp_tranches=[(0, 120.0, 100.0, 2.0, "tp")],
        account=account,
        platform="alpaca",
    )
    order_id = insert_order(
        conn,
        plan_id=row.plan_id,
        alpaca_order_id=order_uid,
        side="BUY",
        order_kind="ENTRY",
        order_type="LIMIT",
        qty=100,
        time_in_force="gtc",
        submitted_at=_NOW,
        tier_index=0,
        limit_price=100.0,
        account=account,
        platform="alpaca",
    )
    insert_fill(
        conn,
        order_id=order_id,
        alpaca_fill_id=fill_uid,
        qty=100,
        price=100.0,
        filled_at=_NOW,
    )
    insert_plan_outcome(
        conn,
        plan_id=row.plan_id,
        exit_kind="TP_HIT",
        closed_at=_NOW,
        blended_entry_price=100.0,
        blended_exit_price=120.0,
        realized_r_multiple=2.0,
    )
    return row.plan_id


class TestResetPaperChain(unittest.TestCase):
    def test_clears_open_chain_keeps_outcomes_and_meta(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            with open_ledger(path) as conn:
                _seed_full_chain(conn)

            with open_ledger(path) as conn:
                counts = reset_paper_chain(conn, account="test", platform="alpaca")

            with open_ledger(path) as conn:
                self.assertEqual(_count(conn, "plans"), 0)
                self.assertEqual(_count(conn, "orders"), 0)
                self.assertEqual(_count(conn, "fills"), 0)
                self.assertEqual(_count(conn, "plan_entries"), 0)
                self.assertEqual(_count(conn, "plan_exits"), 0)
                # KEPT: closed-position history survives.
                self.assertEqual(_count(conn, "plan_outcomes"), 1)
                # KEPT: schema_version meta row survives.
                ver = conn.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()[0]
                self.assertEqual(ver, str(LEDGER_SCHEMA_VERSION))

            # Return value reports the per-table deletion counts.
            self.assertEqual(counts["plans"], 1)
            self.assertEqual(counts["orders"], 1)
            self.assertEqual(counts["fills"], 1)
            self.assertEqual(counts["plan_entries"], 1)
            self.assertEqual(counts["plan_exits"], 1)

    def test_platform_column_still_present_after_reset(self):
        # The reset must not touch schema: the v5 platform column on
        # plans + orders must remain (a fresh plan inserts cleanly).
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            with open_ledger(path) as conn:
                _seed_full_chain(conn)
                reset_paper_chain(conn, account="test", platform="alpaca")
                for table in ("plans", "orders"):
                    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                    self.assertIn("platform", cols)

    def test_idempotent_on_empty_ledger(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            with open_ledger(path) as conn:
                counts = reset_paper_chain(conn, account="test", platform="alpaca")
            self.assertEqual(counts["plans"], 0)
            self.assertEqual(counts["orders"], 0)

    def test_reset_is_account_scoped_main_survives(self):
        # The ledger is a single shared DB holding BOTH the 'main' and
        # 'test' accounts. Resetting 'test' must delete ONLY the test
        # chain; the main account's plans/orders/fills/entries/exits must
        # survive, as must plan_outcomes for both accounts.
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            with open_ledger(path) as conn:
                _seed_full_chain(
                    conn,
                    account="main",
                    ticker="MSFT",
                    order_uid="main-order-1",
                    fill_uid="main-fill-1",
                )
                _seed_full_chain(
                    conn,
                    account="test",
                    ticker="AAPL",
                    order_uid="test-order-1",
                    fill_uid="test-fill-1",
                )

            with open_ledger(path) as conn:
                counts = reset_paper_chain(conn, account="test", platform="alpaca")

            with open_ledger(path) as conn:
                # Only the test chain was deleted.
                self.assertEqual(_count(conn, "plans"), 1)
                self.assertEqual(_count(conn, "orders"), 1)
                self.assertEqual(_count(conn, "fills"), 1)
                self.assertEqual(_count(conn, "plan_entries"), 1)
                self.assertEqual(_count(conn, "plan_exits"), 1)
                # The surviving rows are the MAIN account's.
                surviving = conn.execute("SELECT account FROM plans").fetchone()[0]
                self.assertEqual(surviving, "main")
                self.assertEqual(conn.execute("SELECT account FROM orders").fetchone()[0], "main")
                # plan_outcomes for BOTH accounts survive (closed history).
                self.assertEqual(_count(conn, "plan_outcomes"), 2)

            # Counts report only the test-account deletions.
            self.assertEqual(counts["plans"], 1)
            self.assertEqual(counts["orders"], 1)
            self.assertEqual(counts["fills"], 1)
            self.assertEqual(counts["plan_entries"], 1)
            self.assertEqual(counts["plan_exits"], 1)

    def test_unknown_account_raises(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            with open_ledger(path) as conn:
                with self.assertRaises(ValueError):
                    reset_paper_chain(conn, account="bogus", platform="alpaca")


def _count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


if __name__ == "__main__":
    unittest.main()

"""Tests for the paper-trade report aggregator.

Builds tiny ledgers via the public helpers (insert_planned / insert_order /
insert_fill / insert_plan_outcome / insert_shadow) and asserts that the
Report dataclass surfaces what each query is supposed to surface. No live
Alpaca calls — report is a pure read-only aggregation over the SQLite
ledger.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from alphalens_pipeline.paper.ledger import (
    insert_fill,
    insert_order,
    insert_plan_outcome,
    insert_planned,
    insert_shadow,
    open_ledger,
)
from alphalens_pipeline.paper.report import build_report


def _seed_plan(
    conn,
    *,
    brief_date: dt.date,
    ticker: str,
    qty: int = 27,
    limit_price: float = 100.0,
    account: str = "main",
):
    return insert_planned(
        conn,
        brief_date=brief_date,
        ticker=ticker,
        theme="ai-infra",
        planned_at=dt.datetime.now(dt.UTC),
        suggested_size_pct=5.0,
        scale_factor=0.05,
        final_size_pct=0.25,
        paper_equity=1_000_000.0,
        total_notional=2500.0,
        gross_notional=qty * limit_price,
        disaster_stop=80.0,
        order_ttl_days=10,
        tiers=[(0, limit_price, qty, 100.0, "t0")],
        tp_tranches=[(0, 120.0, 100.0, 2.0, "tp")],
        account=account,
    )


class _ReportTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.ledger = self.tmpdir / "ledger.db"

    def tearDown(self):
        self._tmp.cleanup()


class TestEmptyLedger(_ReportTestBase):
    def test_empty_ledger_produces_zero_report(self):
        """A freshly created ledger has no plans/orders/fills/outcomes —
        report must surface all zeros without crashing."""
        report = build_report(self.ledger)
        self.assertEqual(report.summary.n_plans_planned, 0)
        self.assertEqual(report.summary.n_plans_blocked, 0)
        self.assertEqual(report.summary.n_shadowed, 0)
        self.assertEqual(report.summary.n_entries_submitted, 0)
        self.assertEqual(report.summary.n_entries_filled, 0)
        self.assertEqual(report.summary.n_outcomes, 0)
        self.assertEqual(report.candidates, ())


class TestPlanCounts(_ReportTestBase):
    def test_counts_planned_vs_blocked(self):
        """plans.status splits into PLANNED + BLOCKED + SKIPPED. Each
        category lands in its own counter."""
        d = dt.date(2026, 5, 28)
        with open_ledger(self.ledger) as conn:
            _seed_plan(conn, brief_date=d, ticker="NVDA")
            # Insert a BLOCKED row by raw INSERT (insert_planned only writes PLANNED).
            conn.execute(
                """INSERT INTO plans(brief_date, ticker, theme, planned_at,
                                     suggested_size_pct, scale_factor, final_size_pct,
                                     paper_equity, total_notional, gross_notional,
                                     disaster_stop, order_ttl_days, status, block_reason, account)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'BLOCKED', 'gross_cap', 'main')""",
                (
                    d.isoformat(),
                    "TSLA",
                    "ev",
                    dt.datetime.now(dt.UTC).isoformat(),
                    5.0,
                    0.05,
                    0.25,
                    1_000_000.0,
                    2500.0,
                    2500.0,
                    80.0,
                    10,
                ),
            )

        report = build_report(self.ledger)
        self.assertEqual(report.summary.n_plans_planned, 1)
        self.assertEqual(report.summary.n_plans_blocked, 1)

    def test_shadow_breakdown_by_reason(self):
        """shadow_log rows aggregate per reason — operator wants to know
        which gates triggered the most."""
        d = dt.date(2026, 5, 28)
        with open_ledger(self.ledger) as conn:
            insert_shadow(conn, brief_date=d, ticker="AAA", theme="t", reason="not_verified")
            insert_shadow(conn, brief_date=d, ticker="BBB", theme="t", reason="not_verified")
            insert_shadow(conn, brief_date=d, ticker="CCC", theme="t", reason="no_trade_setup")

        report = build_report(self.ledger)
        self.assertEqual(report.summary.n_shadowed, 3)
        self.assertEqual(report.summary.shadow_by_reason, {"not_verified": 2, "no_trade_setup": 1})


class TestOrderLifecycle(_ReportTestBase):
    def test_submitted_entry_counted_unfilled(self):
        d = dt.date(2026, 5, 28)
        ts = dt.datetime.now(dt.UTC)
        with open_ledger(self.ledger) as conn:
            row = _seed_plan(conn, brief_date=d, ticker="NVDA")
            insert_order(
                conn,
                plan_id=row.plan_id,
                alpaca_order_id="ord-1",
                side="BUY",
                order_kind="ENTRY",
                tier_index=0,
                order_type="LIMIT",
                qty=27,
                limit_price=100.0,
                time_in_force="gtc",
                submitted_at=ts,
            )

        report = build_report(self.ledger)
        self.assertEqual(report.summary.n_entries_submitted, 1)
        self.assertEqual(report.summary.n_entries_filled, 0)

    def test_filled_entry_with_fill_row(self):
        d = dt.date(2026, 5, 28)
        ts = dt.datetime.now(dt.UTC)
        with open_ledger(self.ledger) as conn:
            row = _seed_plan(conn, brief_date=d, ticker="NVDA")
            order_id = insert_order(
                conn,
                plan_id=row.plan_id,
                alpaca_order_id="ord-1",
                side="BUY",
                order_kind="ENTRY",
                tier_index=0,
                order_type="LIMIT",
                qty=27,
                limit_price=100.0,
                time_in_force="gtc",
                submitted_at=ts,
                status="FILLED",
            )
            insert_fill(
                conn,
                order_id=order_id,
                alpaca_fill_id="fill-1",
                qty=27,
                price=99.5,
                filled_at=ts,
            )

        report = build_report(self.ledger)
        self.assertEqual(report.summary.n_entries_filled, 1)
        self.assertEqual(report.summary.n_fills, 1)
        # Per-candidate row shows the fill aggregated.
        self.assertEqual(len(report.candidates), 1)
        cand = report.candidates[0]
        self.assertEqual(cand.ticker, "NVDA")
        self.assertEqual(cand.entry_filled_qty, 27)
        self.assertEqual(cand.entry_planned_qty, 27)
        self.assertAlmostEqual(cand.blended_entry_price, 99.5)

    def test_exit_orders_counted_by_kind(self):
        d = dt.date(2026, 5, 28)
        ts = dt.datetime.now(dt.UTC)
        with open_ledger(self.ledger) as conn:
            row = _seed_plan(conn, brief_date=d, ticker="NVDA")
            insert_order(
                conn,
                plan_id=row.plan_id,
                alpaca_order_id="tp-1",
                side="SELL",
                order_kind="TP",
                tranche_index=0,
                order_type="LIMIT",
                qty=27,
                limit_price=120.0,
                time_in_force="gtc",
                submitted_at=ts,
            )
            insert_order(
                conn,
                plan_id=row.plan_id,
                alpaca_order_id="sl-1",
                side="SELL",
                order_kind="SL",
                order_type="STOP",
                qty=27,
                stop_price=80.0,
                time_in_force="gtc",
                submitted_at=ts,
            )

        report = build_report(self.ledger)
        self.assertEqual(report.summary.n_tp_orders, 1)
        self.assertEqual(report.summary.n_sl_orders, 1)


class TestOutcomesAndRMultiple(_ReportTestBase):
    def _seed_closed_plan(self, conn, *, ticker: str, exit_kind: str, r: float):
        """Helper: PLANNED row → ENTRY FILLED → outcome row with a given R."""
        d = dt.date(2026, 5, 28)
        ts = dt.datetime.now(dt.UTC)
        row = _seed_plan(conn, brief_date=d, ticker=ticker)
        order_id = insert_order(
            conn,
            plan_id=row.plan_id,
            alpaca_order_id=f"e-{ticker}",
            side="BUY",
            order_kind="ENTRY",
            tier_index=0,
            order_type="LIMIT",
            qty=10,
            limit_price=100.0,
            time_in_force="gtc",
            submitted_at=ts,
            status="FILLED",
        )
        insert_fill(
            conn,
            order_id=order_id,
            alpaca_fill_id=f"f-{ticker}",
            qty=10,
            price=100.0,
            filled_at=ts,
        )
        insert_plan_outcome(
            conn,
            plan_id=row.plan_id,
            exit_kind=exit_kind,
            first_fill_at=ts,
            last_exit_at=ts,
            blended_entry_price=100.0,
            blended_exit_price=100.0 + r * 20.0,  # risk=20 (entry-stop)
            realized_r_multiple=r,
            closed_at=ts,
        )

    def test_outcome_counts_by_kind(self):
        with open_ledger(self.ledger) as conn:
            self._seed_closed_plan(conn, ticker="A", exit_kind="TP_HIT", r=1.5)
            self._seed_closed_plan(conn, ticker="B", exit_kind="TP_HIT", r=2.0)
            self._seed_closed_plan(conn, ticker="C", exit_kind="SL_HIT", r=-1.0)
            self._seed_closed_plan(conn, ticker="D", exit_kind="UNFILLED", r=0.0)

        report = build_report(self.ledger)
        self.assertEqual(report.summary.n_outcomes, 4)
        self.assertEqual(report.summary.outcomes_by_kind["TP_HIT"], 2)
        self.assertEqual(report.summary.outcomes_by_kind["SL_HIT"], 1)
        self.assertEqual(report.summary.outcomes_by_kind["UNFILLED"], 1)

    def test_r_multiple_stats_over_closed_plans(self):
        with open_ledger(self.ledger) as conn:
            self._seed_closed_plan(conn, ticker="A", exit_kind="TP_HIT", r=1.5)
            self._seed_closed_plan(conn, ticker="B", exit_kind="TP_HIT", r=2.0)
            self._seed_closed_plan(conn, ticker="C", exit_kind="SL_HIT", r=-1.0)

        report = build_report(self.ledger)
        # Mean = (1.5 + 2.0 - 1.0) / 3 = 0.8333
        self.assertAlmostEqual(report.summary.r_multiple_mean, 0.8333, places=3)
        # Median over sorted [-1.0, 1.5, 2.0] = 1.5
        self.assertAlmostEqual(report.summary.r_multiple_median, 1.5)
        # Hit rate: TP_HIT / closed (excluding UNFILLED which had no fills)
        # 2 TP_HIT out of 3 with-fill outcomes = 0.6667
        self.assertAlmostEqual(report.summary.hit_rate, 2 / 3, places=3)

    def test_unfilled_outcomes_excluded_from_r_stats(self):
        """UNFILLED outcomes (zero entry fills) carry r=None and must NOT
        skew the R-multiple distribution — the strategy never got exposure
        on those candidates."""
        with open_ledger(self.ledger) as conn:
            self._seed_closed_plan(conn, ticker="A", exit_kind="TP_HIT", r=1.5)
            # Manually insert UNFILLED with null R (matches what
            # exit_manager._write_outcome does for zero-fill plans).
            d = dt.date(2026, 5, 28)
            ts = dt.datetime.now(dt.UTC)
            row = _seed_plan(conn, brief_date=d, ticker="UNFI")
            insert_plan_outcome(
                conn,
                plan_id=row.plan_id,
                exit_kind="UNFILLED",
                first_fill_at=None,
                last_exit_at=None,
                blended_entry_price=None,
                blended_exit_price=None,
                realized_r_multiple=None,
                closed_at=ts,
            )

        report = build_report(self.ledger)
        # Only the TP_HIT row has R=1.5; UNFILLED is excluded.
        self.assertAlmostEqual(report.summary.r_multiple_mean, 1.5)
        self.assertEqual(report.summary.n_r_multiple_observations, 1)


class TestScoping(_ReportTestBase):
    def test_date_filter_scopes_to_single_brief_date(self):
        with open_ledger(self.ledger) as conn:
            _seed_plan(conn, brief_date=dt.date(2026, 5, 27), ticker="A")
            _seed_plan(conn, brief_date=dt.date(2026, 5, 28), ticker="B")
            _seed_plan(conn, brief_date=dt.date(2026, 5, 29), ticker="C")

        report = build_report(self.ledger, brief_date=dt.date(2026, 5, 28))
        self.assertEqual(report.summary.n_plans_planned, 1)
        self.assertEqual(report.candidates[0].ticker, "B")

    def test_account_filter_scopes_to_single_account(self):
        d = dt.date(2026, 5, 28)
        with open_ledger(self.ledger) as conn:
            _seed_plan(conn, brief_date=d, ticker="MAIN", account="main")
            _seed_plan(conn, brief_date=d, ticker="TEST", account="test")

        main_report = build_report(self.ledger, account="main")
        test_report = build_report(self.ledger, account="test")
        self.assertEqual(main_report.summary.n_plans_planned, 1)
        self.assertEqual(main_report.candidates[0].ticker, "MAIN")
        self.assertEqual(test_report.summary.n_plans_planned, 1)
        self.assertEqual(test_report.candidates[0].ticker, "TEST")


class TestCandidateRowOrdering(_ReportTestBase):
    def test_candidates_ordered_by_plan_id(self):
        """Stable ordering matters for diff'able operator output."""
        d = dt.date(2026, 5, 28)
        with open_ledger(self.ledger) as conn:
            _seed_plan(conn, brief_date=d, ticker="Z")
            _seed_plan(conn, brief_date=d, ticker="A")
            _seed_plan(conn, brief_date=d, ticker="M")

        report = build_report(self.ledger)
        # Insertion order (Z, A, M) matches plan_id order.
        self.assertEqual([c.ticker for c in report.candidates], ["Z", "A", "M"])


if __name__ == "__main__":
    unittest.main()

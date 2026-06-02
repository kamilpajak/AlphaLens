"""Tests for the reconciler fills tracker.

Uses a stub AlpacaClient whose ``get_order`` returns synthetic Alpaca
order objects with configurable status + filled_qty + filled_avg_price.
Each test exercises one transition path through the reconciler.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from alphalens_pipeline.paper.ledger import (
    fetch_fills_for_order,
    fetch_orders_for_plan,
    insert_order,
    insert_planned,
    open_ledger,
)
from alphalens_pipeline.paper.reconciler import reconcile_orders


@dataclass
class _StubAlpacaOrder:
    """Mimics the alpaca-py Order object enough for the reconciler.

    Status is passed as a string ('new', 'partially_filled', 'filled', ...)
    matching what the reconciler's ``_map_alpaca_status`` extracts from
    the SDK enum.
    """

    id: str
    status: str
    filled_qty: int = 0
    filled_avg_price: float | None = None


@dataclass
class _StubExitOrder:
    """Mirror of _StubAlpacaOrder for synthetic exit submissions."""

    id: str


@dataclass
class _StubAccount:
    """Minimal account snapshot the gross_guard reads."""

    equity: float = 1_000_000.0
    long_market_value: float = 0.0


class _StubBrokerClient:
    def __init__(self) -> None:
        self.orders_by_id: dict[str, _StubAlpacaOrder] = {}
        self.get_order_calls: list[str] = []
        self.fail_for: set[str] = set()
        # Captures exit submissions so the exit-aware tests can assert
        # what got submitted. Existing reconciler tests don't inspect
        # these but the methods need to exist so process_plan_exit can
        # be called transitively from the reconciler.
        self.exit_submissions: list[dict] = []
        self.canceled_orders: list[str] = []
        self._next_exit_id = 1
        # ticker → live position qty for the SL-convergence sizing read.
        # Unset → get_position returns None → _position_qty falls back to the
        # observed ledger filled qty (the pre-hardening default behaviour).
        self.position_qty_for: dict[str, int] = {}
        # Gross-guard reads account.equity + account.long_market_value
        # post-reconcile per memo §6.1 Path B.
        self.account = _StubAccount()

    def add(self, order: _StubAlpacaOrder) -> None:
        self.orders_by_id[order.id] = order

    def get_order(self, alpaca_order_id: str):
        self.get_order_calls.append(alpaca_order_id)
        if alpaca_order_id in self.fail_for:
            raise RuntimeError(f"stub: simulated SDK error for {alpaca_order_id}")
        return self.orders_by_id[alpaca_order_id]

    def submit_stop_order(self, **kwargs):
        self.exit_submissions.append({"kind": "STOP", **kwargs})
        oid = f"exit-stop-{self._next_exit_id:03d}"
        self._next_exit_id += 1
        return _StubExitOrder(id=oid)

    def submit_limit_order(self, **kwargs):
        self.exit_submissions.append({"kind": "LIMIT", **kwargs})
        oid = f"exit-limit-{self._next_exit_id:03d}"
        self._next_exit_id += 1
        return _StubExitOrder(id=oid)

    def submit_market_order(self, **kwargs):
        self.exit_submissions.append({"kind": "MARKET", **kwargs})
        oid = f"exit-market-{self._next_exit_id:03d}"
        self._next_exit_id += 1
        return _StubExitOrder(id=oid)

    def cancel_order(self, alpaca_order_id: str) -> None:
        self.canceled_orders.append(alpaca_order_id)

    def get_account(self) -> _StubAccount:
        return self.account

    def get_position(self, symbol: str):
        """Live broker position the exit_manager sizes protective orders to.

        Defaults to the ledger-side filled total via ``position_qty_for`` (set
        per-ticker by tests that exercise the SL-convergence path). When unset,
        return None so ``_position_qty`` falls back to the observed ledger qty
        — the same not-yet-modelled-position behaviour the time-stop path
        already tolerated.
        """
        qty = self.position_qty_for.get(symbol)
        if qty is None or qty <= 0:
            return None

        @dataclass
        class _Pos:
            qty: int

        return _Pos(qty=qty)


def _make_plan_with_order(
    ledger_path: Path,
    *,
    alpaca_order_id: str,
    qty: int = 27,
    limit_price: float = 100.0,
    planned_at: dt.datetime | None = None,
    order_ttl_days: int = 10,
    account: str = "main",
    ticker: str = "NVDA",
) -> tuple[int, int]:
    """Seed a PLANNED row + one open ENTRY order. Returns (plan_id, order_id).

    ``planned_at`` defaults to now-UTC. The TTL-sweep tests override it to
    simulate plans whose entry-TTL window has elapsed; the per-order tests
    leave it on the default so the sweep is a no-op.
    """
    ts = planned_at if planned_at is not None else dt.datetime.now(dt.UTC)
    d = dt.date(2026, 5, 28)
    with open_ledger(ledger_path) as conn:
        row = insert_planned(
            conn,
            brief_date=d,
            ticker=ticker,
            theme="ai-infra",
            planned_at=ts,
            suggested_size_pct=5.0,
            scale_factor=0.05,
            final_size_pct=0.25,
            paper_equity=1_000_000.0,
            total_notional=2500.0,
            gross_notional=qty * limit_price,
            disaster_stop=80.0,
            order_ttl_days=order_ttl_days,
            tiers=[(0, limit_price, qty, 100.0, "t0")],
            tp_tranches=[(0, 120.0, 100.0, 2.0, "tp")],
            account=account,
        )
        order_id = insert_order(
            conn,
            plan_id=row.plan_id,
            alpaca_order_id=alpaca_order_id,
            side="BUY",
            order_kind="ENTRY",
            tier_index=0,
            order_type="LIMIT",
            qty=qty,
            limit_price=limit_price,
            time_in_force="gtc",
            submitted_at=ts,
            account=account,
        )
    return row.plan_id, order_id


class _ReconcilerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.ledger = self.tmpdir / "ledger.db"
        self.client = _StubBrokerClient()

    def tearDown(self):
        self._tmp.cleanup()


class TestStatusTransitions(_ReconcilerTestBase):
    def test_status_unchanged_when_alpaca_still_new(self):
        _make_plan_with_order(self.ledger, alpaca_order_id="aaa")
        self.client.add(_StubAlpacaOrder(id="aaa", status="new"))

        report = reconcile_orders(ledger_path=self.ledger, broker=self.client)

        self.assertEqual(report.n_orders_checked, 1)
        self.assertEqual(report.n_orders_transitioned, 0)
        self.assertEqual(report.n_fills_appended, 0)

    def test_new_to_filled_transitions_status(self):
        _, order_id = _make_plan_with_order(self.ledger, alpaca_order_id="bbb")
        self.client.add(
            _StubAlpacaOrder(id="bbb", status="filled", filled_qty=27, filled_avg_price=99.5)
        )

        report = reconcile_orders(ledger_path=self.ledger, broker=self.client)
        self.assertEqual(report.n_orders_transitioned, 1)
        self.assertEqual(report.outcomes[0].prev_status, "SUBMITTED")
        self.assertEqual(report.outcomes[0].new_status, "FILLED")
        self.assertEqual(report.n_fills_appended, 1)

        with open_ledger(self.ledger) as conn:
            fills = fetch_fills_for_order(conn, order_id)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["qty"], 27)
        self.assertAlmostEqual(fills[0]["price"], 99.5)

    def test_canceled_transition_appends_no_fill(self):
        _make_plan_with_order(self.ledger, alpaca_order_id="ccc")
        self.client.add(_StubAlpacaOrder(id="ccc", status="canceled"))

        report = reconcile_orders(ledger_path=self.ledger, broker=self.client)
        self.assertEqual(report.outcomes[0].new_status, "CANCELED")
        self.assertEqual(report.n_fills_appended, 0)

        with open_ledger(self.ledger) as conn:
            orders = fetch_orders_for_plan(conn, 1)
        self.assertEqual(orders[0]["status"], "CANCELED")

    def test_unknown_alpaca_status_falls_through_to_submitted_with_warning(self):
        _, _ = _make_plan_with_order(self.ledger, alpaca_order_id="ddd")
        # 'wat' is not in _ALPACA_STATUS_MAP — reconciler logs warning and
        # treats as SUBMITTED so the ledger stays consistent.
        self.client.add(_StubAlpacaOrder(id="ddd", status="wat"))

        with self.assertLogs("alphalens_pipeline.paper.reconciler", level="WARNING") as cm:
            report = reconcile_orders(ledger_path=self.ledger, broker=self.client)
        self.assertEqual(report.outcomes[0].new_status, "SUBMITTED")
        self.assertTrue(any("unknown Alpaca status" in m for m in cm.output))


class TestPartialFills(_ReconcilerTestBase):
    def test_partial_fill_appends_one_fill_row(self):
        _, order_id = _make_plan_with_order(self.ledger, alpaca_order_id="eee", qty=27)
        self.client.add(
            _StubAlpacaOrder(
                id="eee", status="partially_filled", filled_qty=10, filled_avg_price=99.0
            )
        )

        report = reconcile_orders(ledger_path=self.ledger, broker=self.client)
        self.assertEqual(report.n_fills_appended, 1)
        self.assertEqual(report.outcomes[0].new_status, "PARTIALLY_FILLED")

        with open_ledger(self.ledger) as conn:
            fills = fetch_fills_for_order(conn, order_id)
        self.assertEqual(fills[0]["qty"], 10)

    def test_repoll_with_more_fills_appends_delta_only(self):
        """First poll observes 10 / 27 filled. Second poll observes 27 / 27.
        The reconciler must append ONLY the delta (17), not re-create the
        first 10."""
        _, order_id = _make_plan_with_order(self.ledger, alpaca_order_id="fff", qty=27)

        # Poll 1: 10 filled
        self.client.add(
            _StubAlpacaOrder(
                id="fff", status="partially_filled", filled_qty=10, filled_avg_price=99.0
            )
        )
        reconcile_orders(ledger_path=self.ledger, broker=self.client)

        # Poll 2: now 27 filled (full)
        self.client.orders_by_id["fff"] = _StubAlpacaOrder(
            id="fff", status="filled", filled_qty=27, filled_avg_price=99.4
        )
        report = reconcile_orders(ledger_path=self.ledger, broker=self.client)

        self.assertEqual(report.n_fills_appended, 1)
        with open_ledger(self.ledger) as conn:
            fills = fetch_fills_for_order(conn, order_id)
        self.assertEqual(len(fills), 2)
        self.assertEqual([f["qty"] for f in fills], [10, 17])

    def test_repoll_with_unchanged_state_is_idempotent(self):
        """Reconciler called twice with identical Alpaca state must not
        create duplicate fill rows. The synthetic fill_id keyed on
        cumulative qty enforces this."""
        _make_plan_with_order(self.ledger, alpaca_order_id="ggg", qty=27)
        self.client.add(
            _StubAlpacaOrder(id="ggg", status="filled", filled_qty=27, filled_avg_price=99.5)
        )
        r1 = reconcile_orders(ledger_path=self.ledger, broker=self.client)
        r2 = reconcile_orders(ledger_path=self.ledger, broker=self.client)
        self.assertEqual(r1.n_fills_appended, 1)
        self.assertEqual(r2.n_fills_appended, 0)
        # Second pass also reports zero transitions — order already terminal.
        self.assertEqual(r2.n_orders_checked, 0)


class TestSdkFailureGracefullyContinues(_ReconcilerTestBase):
    def test_failed_get_order_logs_and_skips_rest_continue(self):
        """One open order fails to fetch; reconciler logs + skips it and
        still processes the next open order in the same pass."""
        _, _ = _make_plan_with_order(self.ledger, alpaca_order_id="bad")
        self.client.add(_StubAlpacaOrder(id="bad", status="new"))
        self.client.fail_for.add("bad")

        # Seed a second open order whose plan_id is the same — just need
        # another row in fetch_open_orders.
        ts = dt.datetime.now(dt.UTC)
        with open_ledger(self.ledger) as conn:
            insert_order(
                conn,
                plan_id=1,
                alpaca_order_id="good",
                side="BUY",
                order_kind="ENTRY",
                tier_index=1,
                order_type="LIMIT",
                qty=5,
                limit_price=95.0,
                time_in_force="gtc",
                submitted_at=ts,
            )
        self.client.add(
            _StubAlpacaOrder(id="good", status="filled", filled_qty=5, filled_avg_price=94.9)
        )

        with self.assertLogs("alphalens_pipeline.paper.reconciler", level="WARNING") as cm:
            report = reconcile_orders(ledger_path=self.ledger, broker=self.client)

        self.assertEqual(report.n_orders_checked, 1)
        self.assertEqual(report.outcomes[0].alpaca_order_id, "good")
        self.assertTrue(any("reconcile failed to fetch" in m for m in cm.output))


class TestNoOpenOrders(_ReconcilerTestBase):
    def test_empty_ledger_runs_clean(self):
        report = reconcile_orders(ledger_path=self.ledger, broker=self.client)
        self.assertEqual(report.n_orders_checked, 0)
        self.assertEqual(report.n_orders_transitioned, 0)
        self.assertEqual(report.n_fills_appended, 0)


class TestTtlSweep(_ReconcilerTestBase):
    """Entry-TTL sweep: cancel ENTRY orders on plans whose
    ``order_ttl_days`` (trading-day) window has elapsed without all
    entries terminating.

    Once cancelled, the per-order reconciler loop + ``process_plan_exit``
    chain naturally transitions the plan to an outcome (UNFILLED on
    zero fills, ATTACHED to TP+SL on partial fills) — the sweep just
    breaks the GTC-forever stalemate that blocks the existing state
    machine from running.

    All anchors are XNYS sessions. Fri 2026-05-22 → Fri 2026-05-29 is a
    4-trading-day window (Mon 2026-05-25 is Memorial Day and is skipped).
    Fri 2026-05-29 → Fri 2026-06-05 is a clean 5-trading-day week. The
    fixed anchors are deterministic regardless of when CI runs — under
    the old calendar-day TTL these tests were fragile to wall-clock day-
    of-week, which the PR-B switch to trading-day arithmetic now fixes.
    """

    # Fri 2026-05-29 planned + Fri 2026-06-12 observed = 10 trading days
    # (two clean weeks). Comfortably past any reasonable test TTL (5-10).
    # Used by tests that just want "a stale plan" without caring about
    # the exact elapsed count.
    _STALE_PLANNED = dt.datetime(2026, 5, 29, 16, 0, 0, tzinfo=dt.UTC)
    _STALE_OBSERVED = dt.datetime(2026, 6, 12, 22, 0, 0, tzinfo=dt.UTC)

    def test_fresh_plan_within_ttl_is_not_swept(self):
        """Same-day plan (zero trading days elapsed) with TTL=7 must NOT
        trigger a cancel. Pre-open Friday observed mid-session same
        Friday is the canonical "we just submitted, hold the order"."""
        planned = dt.datetime(2026, 5, 29, 13, 25, 0, tzinfo=dt.UTC)
        observed = dt.datetime(2026, 5, 29, 18, 0, 0, tzinfo=dt.UTC)
        _make_plan_with_order(
            self.ledger,
            alpaca_order_id="fresh",
            planned_at=planned,
            order_ttl_days=7,
        )
        self.client.add(_StubAlpacaOrder(id="fresh", status="new"))

        report = reconcile_orders(
            ledger_path=self.ledger,
            broker=self.client,
            observed_at=observed,
        )

        self.assertEqual(report.n_entries_ttl_canceled, 0)
        self.assertEqual(self.client.canceled_orders, [])

    def test_plan_at_exact_ttl_boundary_is_swept(self):
        """planned_at exactly TTL trading days ago → boundary is
        inclusive (>=). Fri 2026-05-29 → Tue 2026-06-09 = 7 trading
        days (clean week of 5 + Mon/Tue of next week)."""
        planned = dt.datetime(2026, 5, 29, 16, 0, 0, tzinfo=dt.UTC)
        observed = dt.datetime(2026, 6, 9, 22, 0, 0, tzinfo=dt.UTC)
        _make_plan_with_order(
            self.ledger,
            alpaca_order_id="boundary",
            planned_at=planned,
            order_ttl_days=7,
        )
        self.client.add(_StubAlpacaOrder(id="boundary", status="new"))

        report = reconcile_orders(
            ledger_path=self.ledger,
            broker=self.client,
            observed_at=observed,
        )

        self.assertEqual(report.n_entries_ttl_canceled, 1)
        self.assertEqual(self.client.canceled_orders, ["boundary"])

    def test_plan_one_day_before_ttl_is_not_swept(self):
        """planned_at TTL-1 trading days ago → just-before-expiry,
        sweep MUST NOT fire. Pairs with ``test_plan_at_exact_ttl_boundary``
        so the ``>=`` is pinned from both sides — an off-by-one (``>``
        instead of ``>=``) would still pass the boundary test, and an
        off-by-one the other way would still pass within-window.

        Fri 2026-05-29 → Mon 2026-06-08 = 6 trading days (clean week + Mon).
        """
        planned = dt.datetime(2026, 5, 29, 16, 0, 0, tzinfo=dt.UTC)
        observed = dt.datetime(2026, 6, 8, 22, 0, 0, tzinfo=dt.UTC)
        _make_plan_with_order(
            self.ledger,
            alpaca_order_id="just-before",
            planned_at=planned,
            order_ttl_days=7,
        )
        self.client.add(_StubAlpacaOrder(id="just-before", status="new"))

        report = reconcile_orders(
            ledger_path=self.ledger,
            broker=self.client,
            observed_at=observed,
        )

        self.assertEqual(report.n_entries_ttl_canceled, 0)
        self.assertEqual(self.client.canceled_orders, [])

    def test_plan_past_ttl_is_swept(self):
        """planned Fri 2026-05-29 → observed Fri 2026-06-12 = 10
        trading days. With TTL=7, cancel fires."""
        planned = dt.datetime(2026, 5, 29, 16, 0, 0, tzinfo=dt.UTC)
        observed = dt.datetime(2026, 6, 12, 22, 0, 0, tzinfo=dt.UTC)
        _make_plan_with_order(
            self.ledger,
            alpaca_order_id="stale",
            planned_at=planned,
            order_ttl_days=7,
        )
        self.client.add(_StubAlpacaOrder(id="stale", status="new"))

        report = reconcile_orders(
            ledger_path=self.ledger,
            broker=self.client,
            observed_at=observed,
        )

        self.assertEqual(report.n_entries_ttl_canceled, 1)
        self.assertEqual(self.client.canceled_orders, ["stale"])

    def test_window_with_memorial_day_does_not_inflate_count(self):
        """Memorial-Day edge case — the headline reason PR-B exists.

        planned Fri 2026-05-22 16:00 UTC, observed Fri 2026-05-29
        22:00 UTC = 7 calendar days, BUT only 4 trading days (Mon
        2026-05-25 is Memorial Day and skipped). Under the legacy
        calendar-day arithmetic ``(observed.date() - planned.date()).days``
        this returned 7, firing the sweep with TTL=5. The new
        ``trading_days_elapsed`` returns 4, holding the window open
        for the actual remaining ~2-3 trading sessions the trade-setup
        memo intends.

        With TTL=5 (boundary above 4), the sweep MUST NOT fire.
        """
        planned = dt.datetime(2026, 5, 22, 16, 0, 0, tzinfo=dt.UTC)
        observed = dt.datetime(2026, 5, 29, 22, 0, 0, tzinfo=dt.UTC)
        _make_plan_with_order(
            self.ledger,
            alpaca_order_id="memorial",
            planned_at=planned,
            order_ttl_days=5,
        )
        self.client.add(_StubAlpacaOrder(id="memorial", status="new"))

        report = reconcile_orders(
            ledger_path=self.ledger,
            broker=self.client,
            observed_at=observed,
        )

        self.assertEqual(report.n_entries_ttl_canceled, 0)
        self.assertEqual(self.client.canceled_orders, [])

    def test_plan_with_outcome_is_not_swept(self):
        """A plan that already has a plan_outcome row (e.g. UNFILLED
        written by an earlier pass) must NOT trigger another cancel —
        otherwise the sweep would re-cancel already-terminal orders
        every cycle, racking up Alpaca API calls forever."""
        planned = dt.datetime(2026, 5, 29, 16, 0, 0, tzinfo=dt.UTC)
        observed = dt.datetime(2026, 6, 26, 22, 0, 0, tzinfo=dt.UTC)
        _make_plan_with_order(
            self.ledger,
            alpaca_order_id="closed",
            planned_at=planned,
            order_ttl_days=7,
        )
        # Order already CANCELED + plan_outcome already written
        with open_ledger(self.ledger) as conn:
            conn.execute(
                "UPDATE orders SET status = 'CANCELED' WHERE alpaca_order_id = ?", ("closed",)
            )
            from alphalens_pipeline.paper.ledger import insert_plan_outcome

            insert_plan_outcome(
                conn,
                plan_id=1,
                exit_kind="UNFILLED",
                closed_at=observed,
            )

        report = reconcile_orders(
            ledger_path=self.ledger,
            broker=self.client,
            observed_at=observed,
        )

        self.assertEqual(report.n_entries_ttl_canceled, 0)
        self.assertEqual(self.client.canceled_orders, [])

    def test_partial_fills_only_unfilled_tiers_get_swept(self):
        """One tier already FILLED, another still SUBMITTED, TTL elapsed:
        the FILLED tier must NOT be cancelled (already terminal); only the
        SUBMITTED tier goes to Alpaca cancel. The plan then naturally flows
        through exit_manager to ATTACH TP+SL on the partial position."""
        plan_id, _ = _make_plan_with_order(
            self.ledger,
            alpaca_order_id="t0-filled",
            planned_at=self._STALE_PLANNED,
            order_ttl_days=10,
        )
        # Mark first tier FILLED in the local ledger so the sweep skips it.
        with open_ledger(self.ledger) as conn:
            conn.execute(
                "UPDATE orders SET status = 'FILLED' WHERE alpaca_order_id = ?", ("t0-filled",)
            )
            insert_order(
                conn,
                plan_id=plan_id,
                alpaca_order_id="t1-open",
                side="BUY",
                order_kind="ENTRY",
                tier_index=1,
                order_type="LIMIT",
                qty=15,
                limit_price=95.0,
                time_in_force="gtc",
                submitted_at=self._STALE_PLANNED,
            )

        self.client.add(_StubAlpacaOrder(id="t1-open", status="new"))

        report = reconcile_orders(
            ledger_path=self.ledger, broker=self.client, observed_at=self._STALE_OBSERVED
        )

        self.assertEqual(self.client.canceled_orders, ["t1-open"])
        self.assertEqual(report.n_entries_ttl_canceled, 1)

    def test_sweep_is_scoped_by_account(self):
        """A TEST-account plan past TTL must NOT be swept by a MAIN-account
        reconcile pass — the MAIN client cannot cancel TEST UUIDs (would
        404). Symmetric for TEST sweeping MAIN."""
        _make_plan_with_order(
            self.ledger,
            alpaca_order_id="main-stale",
            planned_at=self._STALE_PLANNED,
            order_ttl_days=10,
            account="main",
            ticker="NVDA",
        )
        _make_plan_with_order(
            self.ledger,
            alpaca_order_id="test-stale",
            planned_at=self._STALE_PLANNED,
            order_ttl_days=10,
            account="test",
            ticker="AMD",
        )
        self.client.add(_StubAlpacaOrder(id="main-stale", status="new"))
        self.client.add(_StubAlpacaOrder(id="test-stale", status="new"))

        # account=main pass: only main-stale gets cancelled
        report_main = reconcile_orders(
            ledger_path=self.ledger,
            broker=self.client,
            account="main",
            observed_at=self._STALE_OBSERVED,
        )
        self.assertEqual(self.client.canceled_orders, ["main-stale"])
        self.assertEqual(report_main.n_entries_ttl_canceled, 1)

        # account=test pass: now test-stale gets cancelled too
        report_test = reconcile_orders(
            ledger_path=self.ledger,
            broker=self.client,
            account="test",
            observed_at=self._STALE_OBSERVED,
        )
        self.assertEqual(self.client.canceled_orders, ["main-stale", "test-stale"])
        self.assertEqual(report_test.n_entries_ttl_canceled, 1)

    def test_cancel_failure_is_logged_and_sweep_continues(self):
        """Alpaca cancel failure on one order must NOT abort the sweep —
        log + continue so other expired entries on other plans still get
        their cancel attempt this cycle."""
        plan_id, _ = _make_plan_with_order(
            self.ledger,
            alpaca_order_id="bad-cancel",
            planned_at=self._STALE_PLANNED,
            order_ttl_days=10,
        )
        with open_ledger(self.ledger) as conn:
            insert_order(
                conn,
                plan_id=plan_id,
                alpaca_order_id="good-cancel",
                side="BUY",
                order_kind="ENTRY",
                tier_index=1,
                order_type="LIMIT",
                qty=10,
                limit_price=95.0,
                time_in_force="gtc",
                submitted_at=self._STALE_PLANNED,
            )
        self.client.add(_StubAlpacaOrder(id="bad-cancel", status="new"))
        self.client.add(_StubAlpacaOrder(id="good-cancel", status="new"))
        # Override cancel_order to fail on the first id only.
        original_cancel = self.client.cancel_order

        def selective_cancel(oid: str) -> None:
            if oid == "bad-cancel":
                raise RuntimeError("stub: simulated Alpaca cancel failure")
            original_cancel(oid)

        self.client.cancel_order = selective_cancel  # type: ignore[method-assign]

        with self.assertLogs("alphalens_pipeline.paper.reconciler", level="WARNING") as cm:
            report = reconcile_orders(
                ledger_path=self.ledger, broker=self.client, observed_at=self._STALE_OBSERVED
            )

        self.assertEqual(self.client.canceled_orders, ["good-cancel"])
        self.assertEqual(report.n_entries_ttl_canceled, 1)
        self.assertTrue(any("ttl-sweep cancel failed" in m for m in cm.output))

    def test_end_to_end_ttl_sweep_then_unfilled_outcome(self):
        """End-to-end: TTL'd plan with one ENTRY at SUBMITTED, zero fills.
        After the sweep the per-order loop observes Alpaca status flip to
        'canceled' (simulated) → ledger transitions to CANCELED → exit_manager
        sees entry_phase_settled + zero fills → writes UNFILLED outcome.
        All within one reconcile call."""
        plan_id, _ = _make_plan_with_order(
            self.ledger,
            alpaca_order_id="stale-e2e",
            planned_at=self._STALE_PLANNED,
            order_ttl_days=10,
        )

        # Alpaca returns "canceled" status when polled — simulating that
        # the broker processed our cancel in the same cycle, so the
        # per-order loop transitions the row to CANCELED.
        self.client.add(_StubAlpacaOrder(id="stale-e2e", status="canceled"))

        report = reconcile_orders(
            ledger_path=self.ledger, broker=self.client, observed_at=self._STALE_OBSERVED
        )

        self.assertEqual(report.n_entries_ttl_canceled, 1)
        self.assertEqual(self.client.canceled_orders, ["stale-e2e"])
        # Per-order loop saw 'canceled' from Alpaca → transitions to CANCELED.
        self.assertEqual(report.outcomes[0].new_status, "CANCELED")
        # exit_manager writes UNFILLED outcome in the same pass.
        self.assertEqual(report.n_outcomes_written, 1)
        with open_ledger(self.ledger) as conn:
            cur = conn.execute("SELECT exit_kind FROM plan_outcomes WHERE plan_id = ?", (plan_id,))
            row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["exit_kind"], "UNFILLED")

    def test_all_entries_filled_outside_ttl_proceeds_through_exit_manager(self):
        """A plan whose ENTRY orders are all already FILLED (no sweep
        action needed) must still flow through exit_manager and have
        TP+SL attached in the same reconcile pass — independent of
        whether the TTL window has elapsed or not.

        Without this regression, a refactor that gates the exit-manager
        loop on "TTL sweep made progress" would silently strand fully-
        filled plans without exits. The TTL sweep + exit attachment
        paths are orthogonal; this test pins that.
        """
        plan_id, _ = _make_plan_with_order(
            self.ledger,
            alpaca_order_id="filled-old",
            planned_at=self._STALE_PLANNED,
            order_ttl_days=10,
            qty=27,
        )
        # Mark the entry already FILLED locally and seed a matching fill row
        # so process_plan_exit sees total_entry_filled_qty > 0.
        with open_ledger(self.ledger) as conn:
            conn.execute(
                "UPDATE orders SET status = 'FILLED' WHERE alpaca_order_id = ?",
                ("filled-old",),
            )
            from alphalens_pipeline.paper.ledger import insert_fill

            insert_fill(
                conn,
                order_id=1,
                alpaca_fill_id="seed-fill",
                qty=27,
                price=99.5,
                filled_at=self._STALE_PLANNED,
            )

        # The broker confirms the matching real 27-share position so the
        # convergence path (not the ledger<->broker desync guard) runs.
        self.client.position_qty_for["NVDA"] = 27

        report = reconcile_orders(
            ledger_path=self.ledger, broker=self.client, observed_at=self._STALE_OBSERVED
        )

        # Sweep is a no-op (no SUBMITTED/PARTIALLY_FILLED entry to cancel).
        self.assertEqual(report.n_entries_ttl_canceled, 0)
        # exit_manager attaches TP+SL (1 SL stop-market + 1 TP limit-sell).
        self.assertEqual(report.n_exits_attached, 2)
        # Plan has exits but no outcome yet (exits still SUBMITTED).
        with open_ledger(self.ledger) as conn:
            cur = conn.execute(
                "SELECT order_kind FROM orders WHERE plan_id = ? AND side = 'SELL'",
                (plan_id,),
            )
            exit_kinds = sorted(row["order_kind"] for row in cur.fetchall())
        self.assertEqual(exit_kinds, ["SL", "TP"])

    def test_blocked_plan_is_not_swept(self):
        """A plan whose status is BLOCKED (gross-cap rejected at plan time,
        no Alpaca orders ever submitted) must be skipped by the sweep —
        there's nothing to cancel."""
        ts = self._STALE_PLANNED
        with open_ledger(self.ledger) as conn:
            # Bypass insert_planned (which forces status='PLANNED') by raw INSERT.
            conn.execute(
                """INSERT INTO plans(brief_date, ticker, theme, planned_at,
                                     suggested_size_pct, scale_factor, final_size_pct,
                                     paper_equity, total_notional, gross_notional,
                                     disaster_stop, order_ttl_days, status, block_reason, account)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'BLOCKED', 'gross_cap', 'main')""",
                (
                    "2026-05-28",
                    "TSLA",
                    "ev",
                    ts.isoformat(),
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

        report = reconcile_orders(
            ledger_path=self.ledger, broker=self.client, observed_at=self._STALE_OBSERVED
        )

        self.assertEqual(report.n_entries_ttl_canceled, 0)
        self.assertEqual(self.client.canceled_orders, [])


class _ExitFailingBroker(_StubBrokerClient):
    """Stub whose stop-order submit raises for a chosen symbol, modelling
    Alpaca rejecting an SL with held_for_orders / insufficient qty."""

    def __init__(self, *, fail_stop_for: set[str] | None = None) -> None:
        super().__init__()
        self.fail_stop_for = fail_stop_for or set()

    def submit_stop_order(self, **kwargs):
        if kwargs.get("symbol") in self.fail_stop_for:
            raise RuntimeError(
                "stub APIError: held_for_orders insufficient qty available for order"
            )
        return super().submit_stop_order(**kwargs)


class TestReconcileExitResilience(_ReconcilerTestBase):
    """One plan rejecting its protective SELL must not abort the reconcile
    pass for plans behind it."""

    def _seed_filled_plan(self, *, ticker: str, alpaca_id: str) -> int:
        plan_id, order_id = _make_plan_with_order(
            self.ledger, alpaca_order_id=alpaca_id, ticker=ticker, qty=27
        )
        ts = dt.datetime.now(dt.UTC)
        with open_ledger(self.ledger) as conn:
            conn.execute(
                "UPDATE orders SET status = 'FILLED' WHERE alpaca_order_id = ?",
                (alpaca_id,),
            )
            from alphalens_pipeline.paper.ledger import insert_fill

            insert_fill(
                conn,
                order_id=order_id,
                alpaca_fill_id=f"{alpaca_id}-fill",
                qty=27,
                price=99.5,
                filled_at=ts,
            )
        return plan_id

    def test_one_plan_sl_rejection_does_not_block_others(self):
        bad_plan = self._seed_filled_plan(ticker="BADX", alpaca_id="bad-entry")
        good_plan = self._seed_filled_plan(ticker="GOODX", alpaca_id="good-entry")
        # Both entry orders already FILLED locally → no open orders to poll;
        # exit_manager drives attachment for both touched plans.
        broker = _ExitFailingBroker(fail_stop_for={"BADX"})
        # Both plans hold a real 27-share broker position, so the convergence
        # path runs (not the ledger<->broker desync guard).
        broker.position_qty_for["BADX"] = 27
        broker.position_qty_for["GOODX"] = 27

        # Must NOT raise even though BADX's SL submit errors.
        report = reconcile_orders(ledger_path=self.ledger, broker=broker)

        # The good plan still got its full SL + TP attached.
        with open_ledger(self.ledger) as conn:
            good_exits = sorted(
                row["order_kind"]
                for row in conn.execute(
                    "SELECT order_kind FROM orders WHERE plan_id = ? AND side = 'SELL'",
                    (good_plan,),
                ).fetchall()
            )
            bad_exits = [
                row["order_kind"]
                for row in conn.execute(
                    "SELECT order_kind FROM orders WHERE plan_id = ? AND order_kind = 'SL'",
                    (bad_plan,),
                ).fetchall()
            ]
        self.assertEqual(good_exits, ["SL", "TP"])
        # The bad plan's SL never persisted (submit failed) → retryable.
        self.assertEqual(bad_exits, [])
        # Report still reflects the successful attachments.
        self.assertGreaterEqual(report.n_exits_attached, 2)


if __name__ == "__main__":
    unittest.main()

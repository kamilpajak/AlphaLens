"""Tests for ``alphalens paper reset`` — the broker-agnostic safe reset.

The reset cancels every open broker order, flattens every open position
(market order, opposite side, abs(qty)), polls until the broker reports
flat (handling paper-state lag), then clears the ledger paper-chain
(unless ``--keep-ledger``). Strong safety rails: ``--yes`` is REQUIRED to
mutate; default + ``--dry-run`` only print the plan.

Run via the research unittest discover harness (NOT pytest).
"""

from __future__ import annotations

import datetime as dt
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from alphalens_cli.commands.paper import paper_app
from alphalens_pipeline.paper.ledger import insert_order, insert_planned, open_ledger
from typer.testing import CliRunner

_NOW = dt.datetime(2026, 6, 1, 13, 30, tzinfo=dt.UTC)
_BRIEF_DATE = dt.date(2026, 5, 30)


class _StubBroker:
    """Stateful in-memory broker implementing the EXTENDED protocol.

    Models live broker state: ``cancel_order`` removes an open order;
    ``submit_market_order`` flattens the matching position. ``lag_polls``
    simulates Alpaca paper-state lag — a position must be flattened
    ``lag_polls + 1`` times before it actually disappears, so the reset
    must re-sweep to converge (it cannot give up after one sweep).
    """

    def __init__(self, positions, orders, *, lag_polls=0):
        # symbol -> SimpleNamespace position
        self._positions = {p.symbol: p for p in positions}
        # symbol -> remaining flattens needed before it truly clears.
        self._lag_remaining = {p.symbol: lag_polls for p in positions}
        # id -> order object
        self._orders = {o.id: o for o in orders}
        self.cancel_calls: list[str] = []
        self.market_orders: list[dict] = []

    def list_positions(self):
        return list(self._positions.values())

    def list_open_orders(self):
        return list(self._orders.values())

    def cancel_order(self, order_id):
        self.cancel_calls.append(order_id)
        self._orders.pop(order_id, None)

    def submit_market_order(self, *, symbol, qty, side, time_in_force="day"):
        self.market_orders.append(
            {"symbol": symbol, "qty": qty, "side": side, "time_in_force": time_in_force}
        )
        remaining = self._lag_remaining.get(symbol, 0)
        if remaining > 0:
            # Lag: position lingers, needs another flatten next sweep.
            self._lag_remaining[symbol] = remaining - 1
        else:
            self._positions.pop(symbol, None)
            self._lag_remaining.pop(symbol, None)
        return SimpleNamespace(id=f"flatten-{symbol}")

    # Unused-by-reset members of the protocol (present for completeness).
    def submit_limit_order(self, **kw):  # pragma: no cover
        ...

    def submit_stop_order(self, **kw):  # pragma: no cover
        ...

    def get_account(self):  # pragma: no cover
        ...

    def get_position(self, symbol):  # pragma: no cover
        ...

    def get_order(self, order_id):  # pragma: no cover
        ...


def _pos(symbol, qty, side):
    return SimpleNamespace(symbol=symbol, qty=str(qty), side=side)


def _order(order_id, symbol="AAPL"):
    return SimpleNamespace(id=order_id, symbol=symbol, status="new")


def _seed_ledger(
    path: Path,
    *,
    account: str = "test",
    ticker: str = "AAPL",
    order_uid: str = "alpaca-order-1",
) -> None:
    with open_ledger(path) as conn:
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
        insert_order(
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


def _ledger_counts(path: Path, *, account: str | None = None) -> dict[str, int]:
    with open_ledger(path) as conn:
        if account is None:
            return {
                t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
                for t in ("plans", "orders")
            }
        return {
            t: int(
                conn.execute(f"SELECT COUNT(*) FROM {t} WHERE account = ?", (account,)).fetchone()[
                    0
                ]
            )
            for t in ("plans", "orders")
        }


def _run(args, broker):
    runner = CliRunner()
    with mock.patch(
        "alphalens_pipeline.paper.broker.get_default_broker_client",
        return_value=broker,
    ):
        result = runner.invoke(paper_app, args)
    return result


class TestResetHappyPath(unittest.TestCase):
    def test_cancels_flattens_polls_and_clears_ledger(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            _seed_ledger(path)
            # 2 open orders + 2 positions (one long, one short).
            broker = _StubBroker(
                positions=[_pos("AAPL", 100, "long"), _pos("TSLA", -50, "short")],
                orders=[_order("o1"), _order("o2")],
            )
            result = _run(
                ["reset", "--account", "test", "--yes", "--ledger", str(path)],
                broker,
            )
            self.assertEqual(result.exit_code, 0, result.stdout)
            # Every open order cancelled.
            self.assertEqual(set(broker.cancel_calls), {"o1", "o2"})
            # One market order per position, correct side + abs qty.
            by_symbol = {m["symbol"]: m for m in broker.market_orders}
            self.assertEqual(by_symbol["AAPL"]["side"], "sell")  # long -> SELL
            self.assertEqual(by_symbol["AAPL"]["qty"], 100)
            self.assertEqual(by_symbol["TSLA"]["side"], "buy")  # short -> BUY cover
            self.assertEqual(by_symbol["TSLA"]["qty"], 50)  # abs, never negative
            # Ledger cleared.
            counts = _ledger_counts(path)
            self.assertEqual(counts["plans"], 0)
            self.assertEqual(counts["orders"], 0)
            # Final verify line reports flat.
            self.assertIn("positions=0", result.stdout)
            self.assertIn("open orders=0", result.stdout.lower())


class TestResetSafetyRails(unittest.TestCase):
    def test_missing_yes_does_not_mutate(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            _seed_ledger(path)
            broker = _StubBroker(
                positions=[_pos("AAPL", 100, "long")],
                orders=[_order("o1")],
            )
            result = _run(
                ["reset", "--account", "test", "--ledger", str(path)],
                broker,
            )
            self.assertEqual(result.exit_code, 0, result.stdout)
            self.assertEqual(broker.cancel_calls, [])
            self.assertEqual(broker.market_orders, [])
            # Ledger untouched.
            self.assertEqual(_ledger_counts(path)["orders"], 1)
            self.assertIn("--yes", result.stdout)

    def test_dry_run_does_not_mutate(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            _seed_ledger(path)
            broker = _StubBroker(
                positions=[_pos("AAPL", 100, "long")],
                orders=[_order("o1")],
            )
            result = _run(
                ["reset", "--account", "test", "--dry-run", "--yes", "--ledger", str(path)],
                broker,
            )
            self.assertEqual(result.exit_code, 0, result.stdout)
            # --dry-run wins even with --yes: no mutation.
            self.assertEqual(broker.cancel_calls, [])
            self.assertEqual(broker.market_orders, [])
            self.assertEqual(_ledger_counts(path)["orders"], 1)


class TestResetPaperStateLag(unittest.TestCase):
    def test_re_sweeps_until_flat(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            _seed_ledger(path)
            # The position lingers as a "ghost" for one extra poll after
            # the flatten (Alpaca paper-state lag). The reset must re-sweep
            # and converge, not give up after the first sweep.
            broker = _StubBroker(
                positions=[_pos("AAPL", 100, "long")],
                orders=[_order("o1")],
                lag_polls=1,
            )
            result = _run(
                ["reset", "--account", "test", "--yes", "--ledger", str(path)],
                broker,
            )
        self.assertEqual(result.exit_code, 0, result.stdout)
        # It re-issued the flatten on the lag sweep (>= 2 market orders).
        self.assertGreaterEqual(len(broker.market_orders), 2)
        self.assertIn("positions=0", result.stdout)


class TestResetKeepLedger(unittest.TestCase):
    def test_keep_ledger_skips_clear(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            _seed_ledger(path)
            broker = _StubBroker(
                positions=[_pos("AAPL", 100, "long")],
                orders=[_order("o1")],
            )
            result = _run(
                ["reset", "--account", "test", "--yes", "--keep-ledger", "--ledger", str(path)],
                broker,
            )
            self.assertEqual(result.exit_code, 0, result.stdout)
            # Broker still reset.
            self.assertEqual(broker.cancel_calls, ["o1"])
            self.assertEqual(len(broker.market_orders), 1)
            # Ledger NOT cleared.
            self.assertEqual(_ledger_counts(path)["orders"], 1)


class TestResetBackup(unittest.TestCase):
    def test_backup_file_created(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            _seed_ledger(path)
            broker = _StubBroker(positions=[], orders=[])
            result = _run(
                ["reset", "--account", "test", "--yes", "--ledger", str(path)],
                broker,
            )
            self.assertEqual(result.exit_code, 0, result.stdout)
            backups = list(Path(tmp).glob("ledger.db.*.bak"))
            self.assertEqual(len(backups), 1, f"expected one .bak, found {backups}")


class TestResetAccountScoped(unittest.TestCase):
    def test_reset_test_account_leaves_main_chain_intact(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            # One shared ledger holding BOTH accounts.
            _seed_ledger(path, account="main", ticker="MSFT", order_uid="main-order-1")
            _seed_ledger(path, account="test", ticker="AAPL", order_uid="test-order-1")
            broker = _StubBroker(positions=[], orders=[])
            result = _run(
                ["reset", "--account", "test", "--yes", "--ledger", str(path)],
                broker,
            )
            self.assertEqual(result.exit_code, 0, result.stdout)
            # Test chain gone, main chain survives.
            self.assertEqual(_ledger_counts(path, account="test"), {"plans": 0, "orders": 0})
            self.assertEqual(_ledger_counts(path, account="main"), {"plans": 1, "orders": 1})
            # Preview counts (printed in the summary line) reflect ONLY the
            # test account — not the combined main+test total.
            self.assertIn("ledger_plans=1 ledger_orders=1", result.stdout)


class TestResetFractionalPosition(unittest.TestCase):
    def test_fractional_position_is_flattened_not_skipped(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            _seed_ledger(path)
            # A 0.5-share long: int-truncation would drop it; it must be
            # flattened with a fractional abs qty, correct side.
            broker = _StubBroker(
                positions=[_pos("AAPL", 0.5, "long")],
                orders=[],
            )
            result = _run(
                ["reset", "--account", "test", "--yes", "--ledger", str(path)],
                broker,
            )
            self.assertEqual(result.exit_code, 0, result.stdout)
            by_symbol = {m["symbol"]: m for m in broker.market_orders}
            self.assertIn("AAPL", by_symbol)  # not skipped
            self.assertEqual(by_symbol["AAPL"]["side"], "sell")  # long -> SELL
            self.assertEqual(by_symbol["AAPL"]["qty"], 0.5)  # fractional abs qty
            self.assertIn("positions=0", result.stdout)


class TestResetMissingLedger(unittest.TestCase):
    def test_missing_ledger_skips_clear_without_creating_db(self):
        with TemporaryDirectory() as tmp:
            # A path that does NOT exist (e.g. a mistyped --ledger). The
            # clear must NOT silently create + 'clear' a fresh empty DB.
            missing = Path(tmp) / "does_not_exist.db"
            broker = _StubBroker(positions=[], orders=[])
            result = _run(
                ["reset", "--account", "test", "--yes", "--ledger", str(missing)],
                broker,
            )
            self.assertEqual(result.exit_code, 0, result.stdout)
            self.assertIn("nothing to clear", result.stdout)
            # No DB file was created at the mistyped path.
            self.assertFalse(missing.exists())


class TestResetBrokerResolutionFailure(unittest.TestCase):
    def test_unresolvable_broker_exits_nonzero(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            _seed_ledger(path)
            runner = CliRunner()
            with mock.patch(
                "alphalens_pipeline.paper.broker.get_default_broker_client",
                side_effect=ValueError("no ALPACA_API_KEY"),
            ):
                result = runner.invoke(
                    paper_app,
                    ["reset", "--account", "test", "--yes", "--ledger", str(path)],
                )
            self.assertNotEqual(result.exit_code, 0)
            # Ledger untouched on a resolution failure.
            self.assertEqual(_ledger_counts(path)["orders"], 1)


if __name__ == "__main__":
    unittest.main()

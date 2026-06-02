"""Unit tests for the broker-agnostic reset orchestrator (paper/reset.py).

Covers the flatten-instruction sign/side correctness (never flip a long
to short), the snapshot read, and converge-until-flat polling under
paper-state lag — all against an in-memory stub, no Alpaca / sqlite.

Run via the research unittest discover harness (NOT pytest).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from alphalens_pipeline.paper.reset import (
    DEFAULT_MAX_SWEEPS,
    execute_reset,
    snapshot_reset,
)


def _pos(symbol, qty, side="long"):
    return SimpleNamespace(symbol=symbol, qty=str(qty), side=side)


def _order(order_id):
    return SimpleNamespace(id=order_id, status="new")


class _StatefulBroker:
    def __init__(self, positions, orders, *, lag_polls=0):
        self._positions = {p.symbol: p for p in positions}
        self._lag = {p.symbol: lag_polls for p in positions}
        self._orders = {o.id: o for o in orders}
        self.cancels: list[str] = []
        self.markets: list[dict] = []

    def list_positions(self):
        return list(self._positions.values())

    def list_open_orders(self):
        return list(self._orders.values())

    def cancel_order(self, order_id):
        self.cancels.append(order_id)
        self._orders.pop(order_id, None)

    def submit_market_order(self, *, symbol, qty, side, time_in_force="day"):
        self.markets.append({"symbol": symbol, "qty": qty, "side": side})
        remaining = self._lag.get(symbol, 0)
        if remaining > 0:
            self._lag[symbol] = remaining - 1
        else:
            self._positions.pop(symbol, None)
        return SimpleNamespace(id=f"m-{symbol}")


class TestFlattenSign(unittest.TestCase):
    def test_long_flattens_with_sell_and_abs_qty(self):
        snap = snapshot_reset(_StatefulBroker([_pos("AAPL", 100, "long")], []))
        self.assertEqual(len(snap.flatten_plan), 1)
        instr = snap.flatten_plan[0]
        self.assertEqual(instr.side, "sell")
        self.assertEqual(instr.qty, 100)

    def test_short_flattens_with_buy_and_abs_qty(self):
        snap = snapshot_reset(_StatefulBroker([_pos("TSLA", -50, "short")], []))
        instr = snap.flatten_plan[0]
        self.assertEqual(instr.side, "buy")  # cover the short
        self.assertEqual(instr.qty, 50)  # abs, never negative -> never flips to short

    def test_zero_qty_position_is_skipped(self):
        snap = snapshot_reset(_StatefulBroker([_pos("ZZZ", 0, "long")], []))
        self.assertEqual(snap.flatten_plan, [])

    def test_fractional_long_is_flattened_with_fractional_qty(self):
        # Alpaca paper can hold fractional shares; int-truncation would
        # drop a 0.5 position (0.5 -> 0 -> skipped). It must flatten with
        # a fractional abs qty + the correct side, not be skipped.
        snap = snapshot_reset(_StatefulBroker([_pos("AAPL", 0.5, "long")], []))
        self.assertEqual(len(snap.flatten_plan), 1)
        instr = snap.flatten_plan[0]
        self.assertEqual(instr.side, "sell")
        self.assertEqual(instr.qty, 0.5)

    def test_fractional_short_is_covered_with_fractional_qty(self):
        snap = snapshot_reset(_StatefulBroker([_pos("TSLA", -0.25, "short")], []))
        instr = snap.flatten_plan[0]
        self.assertEqual(instr.side, "buy")
        self.assertEqual(instr.qty, 0.25)


class TestExecuteReset(unittest.TestCase):
    def test_converges_first_sweep_when_no_lag(self):
        broker = _StatefulBroker([_pos("AAPL", 10, "long")], [_order("o1")])
        result = execute_reset(broker)
        self.assertTrue(result.is_flat)
        self.assertEqual(result.sweeps_used, 1)
        self.assertEqual(result.n_cancel_calls, 1)
        self.assertEqual(result.n_flatten_calls, 1)

    def test_re_sweeps_under_lag(self):
        # Needs flattening twice (lag_polls=1) before it clears.
        broker = _StatefulBroker([_pos("AAPL", 10, "long")], [], lag_polls=1)
        result = execute_reset(broker)
        self.assertTrue(result.is_flat)
        self.assertGreaterEqual(result.sweeps_used, 2)
        self.assertGreaterEqual(result.n_flatten_calls, 2)

    def test_gives_up_after_max_sweeps_and_reports_not_flat(self):
        # lag larger than the budget -> never converges; must report not flat
        # rather than loop forever.
        broker = _StatefulBroker([_pos("AAPL", 10, "long")], [], lag_polls=DEFAULT_MAX_SWEEPS + 5)
        result = execute_reset(broker, max_sweeps=2)
        self.assertFalse(result.is_flat)
        self.assertEqual(result.sweeps_used, 2)

    def test_cancel_failure_does_not_abort_sweep(self):
        class _FlakyBroker(_StatefulBroker):
            def cancel_order(self, order_id):
                # The 'bad' order is already gone broker-side: remove it but
                # raise (the race the best-effort loop must tolerate). The
                # 'good' order in the SAME sweep must still be cancelled.
                self.cancels.append(order_id)
                self._orders.pop(order_id, None)
                if order_id == "bad":
                    raise RuntimeError("already cancelled")

        broker = _FlakyBroker([], [_order("bad"), _order("good")])
        result = execute_reset(broker)
        self.assertTrue(result.is_flat)
        self.assertIn("good", broker.cancels)


if __name__ == "__main__":
    unittest.main()

"""G6/G9 cancel-then-verify for entry-trail terminals (PR-T2b guards).

A SUSPENDED (G9 deep-decline) or EXPIRED (TTL) terminal must never leave a
resting ``-entry-`` native trailing order alive on the broker's book. The wire
takes it off the book (cancel is risk-reducing) and then RE-READS it (G6): an
order that FILLED during the cancel race is NOT the clock/depth terminal it
became in the engine — it is a ``fired`` fill (already covered by the fire-arm
planned disaster line), so the journal records ``fired`` with the real order id,
never a ``suspended``/``expired`` marker against a live fill.

The only reachable way a non-armed tier holds a resting order is the G3
arm-in-progress window: a ``trail_armed`` write-ahead line with a NULL id (POST
done, id-journal lost) while the real order rests at the broker. That tier stays
ACTIVE (the fold shows ``armed_order_id=None``), so a later deep-decline / TTL
tick drives the engine to a terminal while the order is still on the book.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager import entry_trails

# Shared hermetic fixtures live in the T1c wiring tests.
from tests.brokers.automanager.test_entry_watch_wiring import (
    _ALLOW,
    _FakeFeed,
    _journal,
    _lines,
    _planned_journal,
    _RecordingBroker,
    _seed_watch,
    _watch_deps,
)

_CRID = "KO-2026-07-20-entry-t0"
_FIRE_RID = "KO-2026-07-20-entry-t0-fire"


def _order_state(
    *, order_id: str, external_reference: str, status: Any = None, filled_quantity: float = 0.0
) -> Any:
    from broker_contract.contract import OrderStatus

    return type(
        "OS",
        (),
        {
            "order_id": order_id,
            "external_reference": external_reference,
            "side": "BUY",
            "status": status if status is not None else OrderStatus.WORKING,
            "filled_quantity": filled_quantity,
        },
    )()


_ARMED_CEILING = 10.07
"""The ceiling the write-ahead line journals (#1317) — known even while the arm
is still in progress, because the geometry exists before the POST."""


def _seed_arm_in_progress(path: Any, *, next_tier_limit: float | None) -> None:
    """A watch_open + a NULL-id ``trail_armed`` write-ahead line — the G3
    arm-in-progress state that keeps the tier ACTIVE with a possibly-resting
    order at the broker."""
    _seed_watch(path, crid=_CRID, limit=10.0, next_tier_limit=next_tier_limit)
    entry_trails.append_entry_trail_line(
        {
            "kind": entry_trails.KIND_TRAIL_ARMED,
            "crid": _CRID,
            "order_id": None,
            entry_trails.KEY_CEILING: _ARMED_CEILING,
        }
    )


class TestSuspendCancelsRestingTrail(unittest.TestCase):
    def _run(self, deps: cl.LoopDeps, price: float | None, feed: dict[int, float | None]) -> None:
        feed[307] = price
        with mock.patch.dict("os.environ", _ALLOW, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())

    def test_g9_suspend_cancels_the_resting_entry_order(self) -> None:
        # A deep-decline below the next tier suspends the tier; the resting
        # -entry- order (arm-in-progress) is taken off the book, re-read WORKING
        # (not filled), so the suspended terminal stands.
        path = _journal(self)
        _planned_journal(self)
        _seed_arm_in_progress(path, next_tier_limit=9.5)
        broker = _RecordingBroker()
        broker.open_orders = [_order_state(order_id="TR-9", external_reference=_FIRE_RID)]
        broker.order_states["TR-9"] = _order_state(order_id="TR-9", external_reference=_FIRE_RID)
        prices: dict[int, float | None] = {}
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)

        self._run(deps, 9.40, prices)  # touch + below next tier 9.5 -> suspend

        self.assertIn("TR-9", broker.cancels, "the resting trail is cancelled on suspend (G9)")
        kinds = [ln["kind"] for ln in _lines(path)]
        self.assertIn(entry_trails.KIND_SUSPENDED, kinds)
        self.assertNotIn(entry_trails.KIND_FIRED, kinds)

    def test_filled_during_cancel_becomes_fired_not_suspended(self) -> None:
        # G6: the cancel races a fill — the re-read shows FILLED, so the tier is
        # `fired` (covered by the fire-arm planned line), never `suspended`.
        from broker_contract.contract import OrderStatus

        path = _journal(self)
        _planned_journal(self)
        _seed_arm_in_progress(path, next_tier_limit=9.5)
        broker = _RecordingBroker()
        broker.open_orders = [_order_state(order_id="TR-9", external_reference=_FIRE_RID)]
        broker.order_states["TR-9"] = _order_state(
            order_id="TR-9",
            external_reference=_FIRE_RID,
            status=OrderStatus.FILLED,
            filled_quantity=100.0,
        )
        prices: dict[int, float | None] = {}
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)

        self._run(deps, 9.40, prices)

        kinds = [ln["kind"] for ln in _lines(path)]
        self.assertIn(entry_trails.KIND_FIRED, kinds, "a filled-during-cancel order becomes fired")
        self.assertNotIn(entry_trails.KIND_SUSPENDED, kinds)
        fired = next(ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_FIRED)
        self.assertEqual(fired["order_id"], "TR-9")
        self.assertEqual(fired["realized_qty"], 100.0)
        # deliverable T1d: the terminal measurement joins the REAL order id.
        self.assertEqual(fired["measurement"]["order_id"], "TR-9")
        # #1317 review: this is the SECOND writer of a `fired` line, and its
        # measurement must carry the same ceiling keys as the reconcile path —
        # otherwise a real fill is invisible to the breach detector, and a reader
        # cannot tell a missing key from a null verdict. Here the ceiling IS
        # known (the write-ahead journaled it) while the fill price is NOT: the
        # open-orders re-read carries a quantity, never an execution price. So
        # the honest stamp is a known ceiling with NO VERDICT beside it.
        measurement = fired["measurement"]
        self.assertIn("ceiling", measurement)
        self.assertIn("ceiling_breach", measurement)
        self.assertEqual(measurement["ceiling"], _ARMED_CEILING)
        self.assertIsNone(measurement["ceiling_breach"])

    def test_no_resting_order_leaves_the_suspend_untouched(self) -> None:
        # The common case: no -entry- order rests, so nothing is cancelled and the
        # suspended terminal is written exactly as before.
        path = _journal(self)
        _planned_journal(self)
        _seed_arm_in_progress(path, next_tier_limit=9.5)
        broker = _RecordingBroker()  # open_orders empty
        prices: dict[int, float | None] = {}
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)

        self._run(deps, 9.40, prices)

        self.assertEqual(broker.cancels, [])
        self.assertIn(entry_trails.KIND_SUSPENDED, [ln["kind"] for ln in _lines(path)])


class TestExpiryCancelsRestingTrail(unittest.TestCase):
    def test_ttl_expiry_cancels_the_resting_entry_order(self) -> None:
        path = _journal(self)
        _planned_journal(self)
        # Past TTL window + arm-in-progress + a resting order.
        _seed_watch(
            path,
            crid=_CRID,
            limit=10.0,
            next_tier_limit=None,
            window_end="2000-01-01T00:00:00+00:00",
        )
        entry_trails.append_entry_trail_line(
            {"kind": entry_trails.KIND_TRAIL_ARMED, "crid": _CRID, "order_id": None}
        )
        broker = _RecordingBroker()
        broker.open_orders = [_order_state(order_id="TR-9", external_reference=_FIRE_RID)]
        broker.order_states["TR-9"] = _order_state(order_id="TR-9", external_reference=_FIRE_RID)
        prices: dict[int, float | None] = {307: None}  # no price -> expiry still fires
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)
        with mock.patch.dict("os.environ", _ALLOW, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())

        self.assertIn("TR-9", broker.cancels, "the resting DayOrder trail is cancelled on expiry")
        self.assertIn(entry_trails.KIND_EXPIRED, [ln["kind"] for ln in _lines(path)])


if __name__ == "__main__":
    unittest.main()

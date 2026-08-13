"""Fill-reconcile of a RESTING armed entry-trail order (PR-T2b, Finding 1).

Once a tier reaches TRAIL_ARMED with a real order id it is EXCLUDED from the
watch pass (the broker owns the resting native order — it ratchets + fires
server-side). Without a reconcile pass NO terminal ``entry_trails`` line is ever
written on the order's fill / DayOrder-expiry, so ``watching_virtual_gross_acct``
keeps reserving ``limit x qty`` forever AND ``_open_watch_pick_keys`` keeps the
tier occupying capacity forever — the feature arms one pick then jams.

``_run_entry_trail_reconcile_pass`` closes the leak: for every resting armed tier
it asks the broker whether the order still rests (``get_order`` -> WORKING) or has
DISAPPEARED (``get_order`` -> UNKNOWN, since Saxo drops filled/expired/cancelled
from the open-orders view); a disappeared order is disambiguated by ONE audit-log
read (``resolve_order_outcome``). A FILL writes the terminal ``fired`` line
(releasing the reservation + un-jamming capacity in one write); a still-working
order is a no-op; a GONE-but-UNFILLED order (a DayOrder expiry or a raced cancel)
is LEFT for the Rearm phase (Finding 2) — never terminated here.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager import entry_trails
from broker_contract.contract import OrderState, OrderStatus

# Shared hermetic fixtures live in the T1c wiring tests.
from tests.brokers.automanager.test_entry_watch_wiring import (
    _ENV,
    _journal,
    _lines,
    _planned_journal,
    _RecordingBroker,
    _seed_watch,
    _watch_deps,
)

_CRID = "KO-2026-07-20-entry-t0"
_UIC = 307


def _os(
    order_id: str,
    status: OrderStatus,
    *,
    filled_quantity: float = 0.0,
    avg_fill_price: float | None = None,
) -> OrderState:
    return OrderState(
        order_id=order_id,
        status=status,
        instrument=None,
        filled_quantity=filled_quantity,
        raw_status="",
        avg_fill_price=avg_fill_price,
    )


class _ResolvingBroker(_RecordingBroker):
    """A ``_RecordingBroker`` that also resolves a DISAPPEARED order via the audit
    log (``SupportsOrderResolution``) — the reconcile pass's fill disambiguator.

    ``get_order`` answers WORKING while the order rests (present in
    ``order_states``) and UNKNOWN once it is gone (absent) — mirroring Saxo's
    open-orders view dropping every terminal. ``resolve_order_outcome`` returns the
    seeded terminal ``OrderState`` and records that it was called (idempotency)."""

    def __init__(self) -> None:
        super().__init__()
        self.resolutions: dict[str, OrderState] = {}
        self.resolve_calls: list[str] = []

    def get_order(self, order_id: str) -> Any:
        state = self.order_states.get(order_id)
        if state is not None:
            return state
        return _os(order_id, OrderStatus.UNKNOWN)

    def resolve_order_outcome(self, order_id: str) -> OrderState:
        self.resolve_calls.append(order_id)
        return self.resolutions[order_id]


def _seed_armed(path: Any, *, crid: str = _CRID, order_id: str, limit: float = 10.0) -> None:
    """A watch_open + a REAL-id ``trail_armed`` line — a resting native order the
    broker owns (excluded from the watch pass, owned by the reconcile pass)."""
    _seed_watch(path, crid=crid, limit=limit, next_tier_limit=None)
    entry_trails.append_entry_trail_line(
        {
            "kind": entry_trails.KIND_TRAIL_ARMED,
            "crid": crid,
            "order_id": order_id,
            "trigger": 10.05,
        }
    )


def _run(deps: cl.LoopDeps, env: dict[str, str] | None = None) -> None:
    with mock.patch.dict("os.environ", {_ENV: "50"} if env is None else env, clear=True):
        cl._run_entry_trail_reconcile_pass(deps, cl.TickReport())


class TestFilledArmedTierWritesFired(unittest.TestCase):
    def test_filled_order_writes_exactly_one_fired_line_with_realized_qty_and_avg_price(
        self,
    ) -> None:
        path = _journal(self)
        _seed_armed(path, order_id="TR-1", limit=10.0)
        broker = _ResolvingBroker()  # order gone from the book -> resolve FILLED
        broker.resolutions["TR-1"] = _os(
            "TR-1", OrderStatus.FILLED, filled_quantity=100.0, avg_fill_price=10.05
        )
        deps = _watch_deps(None, [], broker=broker)

        _run(deps)

        fired = [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_FIRED]
        self.assertEqual(len(fired), 1, "one fired line released the reservation")
        self.assertEqual(fired[0]["order_id"], "TR-1")
        self.assertEqual(fired[0]["realized_qty"], 100.0)  # G8: realized, not requested
        self.assertEqual(fired[0]["avg_price"], 10.05)
        # T1d: the measurement joins the REAL order id + the realized fill.
        self.assertEqual(fired[0]["measurement"]["order_id"], "TR-1")
        self.assertEqual(fired[0]["measurement"]["avg_price"], 10.05)
        self.assertEqual(fired[0]["measurement"]["tier_limit"], 10.0)

    def test_fired_line_makes_the_fold_terminal_and_releases_the_reservation(self) -> None:
        path = _journal(self)
        _seed_armed(path, order_id="TR-1", limit=10.0)
        broker = _ResolvingBroker()
        broker.resolutions["TR-1"] = _os(
            "TR-1", OrderStatus.FILLED, filled_quantity=100.0, avg_fill_price=10.05
        )
        deps = _watch_deps(None, [], broker=broker)

        # Before: the armed tier still reserves limit x qty (the leak).
        before = entry_trails.read_entry_trail_fold()
        total_before, bad_before = entry_trails.watching_virtual_gross_acct(before)
        self.assertEqual((total_before, bad_before), (1_000.0, 0))

        _run(deps)

        after = entry_trails.read_entry_trail_fold()
        self.assertEqual(after.tiers[_CRID].terminal_kind, entry_trails.KIND_FIRED)
        total_after, bad_after = entry_trails.watching_virtual_gross_acct(after)
        self.assertEqual((total_after, bad_after), (0.0, 0), "the reservation is released")


class TestCapacityUnjammedByFired(unittest.TestCase):
    def test_capacity_is_reached_while_armed_and_freed_after_fired(self) -> None:
        path = _journal(self)
        _seed_armed(path, order_id="TR-1", limit=10.0)
        broker = _ResolvingBroker()
        broker.resolutions["TR-1"] = _os(
            "TR-1", OrderStatus.FILLED, filled_quantity=100.0, avg_fill_price=10.05
        )
        deps = _watch_deps(None, [], broker=broker)

        # A resting armed (non-terminal) tier occupies the single watch slot.
        self.assertTrue(
            cl._entry_watch_capacity_reached(entry_trails.read_entry_trail_fold()),
            "the resting armed tier jams the single watch slot",
        )

        _run(deps)

        # After the fired terminal a NEW pick can open a watch again.
        self.assertFalse(
            cl._entry_watch_capacity_reached(entry_trails.read_entry_trail_fold()),
            "the fired terminal frees the watch slot",
        )


class TestNeverNakedPreservedByReconcile(unittest.TestCase):
    def test_reconcile_leaves_the_planned_disaster_line_intact_and_position_covered(self) -> None:
        from alphalens_pipeline.brokers.automanager.position_manager import PlaceStop

        # Reuse the proven protection fixtures (uic 43070) so the trace mirrors the
        # existing TestEntryTrailNeverNaked end-to-end.
        from tests.brokers.automanager.test_control_loop import _UIC, _pos, _ProtBroker

        path = _journal(self)
        planned_path = _planned_journal(self)
        # Fire-arm already wrote the planned disaster-SL line (never-naked).
        cl._journal_entry_planned_disaster(
            {"disaster_stop": 216.48, "tier_index": 0}, _UIC, f"{_CRID}-fire"
        )
        planned_before = planned_path.read_text()
        _seed_armed(path, order_id="TR-1", limit=10.0)
        broker = _ResolvingBroker()
        broker.resolutions["TR-1"] = _os(
            "TR-1", OrderStatus.FILLED, filled_quantity=100.0, avg_fill_price=10.05
        )
        deps = _watch_deps(None, [], broker=broker)

        _run(deps)

        # The fired write is entry-trails-only: the standalone-stops journal (where
        # the planned disaster line lives) is byte-identical — the reconcile never
        # races or removes the never-naked protection.
        self.assertEqual(planned_path.read_text(), planned_before)
        # ... so once the filled trail becomes a naked long Position the UNCHANGED
        # protection pass still derives the covering SELL disaster stop.
        prot = _ProtBroker(positions=[_pos(100.0)], by_uic={_UIC: _pos(100.0)})
        actions = cl.reconcile_protection(cl.build_protection_view(prot, []))
        places = [a for a in actions if isinstance(a, PlaceStop)]
        self.assertEqual(len(places), 1, "the filled trail is still covered by a disaster stop")
        self.assertEqual(places[0].stop_price, 216.48, "the brief disaster floor from fire-arm")


class TestReconcileIsIdempotent(unittest.TestCase):
    def test_two_passes_write_exactly_one_fired_line_and_resolve_once(self) -> None:
        path = _journal(self)
        _seed_armed(path, order_id="TR-1", limit=10.0)
        broker = _ResolvingBroker()
        broker.resolutions["TR-1"] = _os(
            "TR-1", OrderStatus.FILLED, filled_quantity=100.0, avg_fill_price=10.05
        )
        deps = _watch_deps(None, [], broker=broker)

        _run(deps)
        _run(deps)  # second pass: the tier is terminal -> excluded

        fired = [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_FIRED]
        self.assertEqual(len(fired), 1, "the fired line is written exactly once")
        self.assertEqual(broker.resolve_calls, ["TR-1"], "a terminal tier is not re-resolved")


class TestStillWorkingIsNoop(unittest.TestCase):
    def test_resting_working_order_writes_no_terminal_and_never_resolves(self) -> None:
        path = _journal(self)
        _seed_armed(path, order_id="TR-1", limit=10.0)
        broker = _ResolvingBroker()
        broker.order_states["TR-1"] = _os("TR-1", OrderStatus.WORKING)  # still on the book
        deps = _watch_deps(None, [], broker=broker)

        _run(deps)

        self.assertEqual(
            [ln for ln in _lines(path) if ln["kind"] in entry_trails.ENTRY_TRAIL_TERMINAL_KINDS], []
        )
        self.assertEqual(broker.resolve_calls, [], "a still-resting order is not resolved")


class TestGoneUnfilledLeftForRearm(unittest.TestCase):
    def test_expired_order_writes_no_terminal_and_stays_re_armable(self) -> None:
        # A DayOrder that expired at the close: resolve -> EXPIRED. THIS phase must
        # NOT terminate it — a terminal `expired` line would kill the carried-trough
        # re-arm the Rearm phase (Finding 2 / memo CRITICAL-2) depends on.
        path = _journal(self)
        _seed_armed(path, order_id="TR-1", limit=10.0)
        broker = _ResolvingBroker()
        broker.resolutions["TR-1"] = _os("TR-1", OrderStatus.EXPIRED)
        deps = _watch_deps(None, [], broker=broker)

        _run(deps)

        self.assertEqual(
            [ln for ln in _lines(path) if ln["kind"] in entry_trails.ENTRY_TRAIL_TERMINAL_KINDS],
            [],
            "a gone-but-unfilled order is left for the Rearm phase, never terminated",
        )
        after = entry_trails.read_entry_trail_fold()
        self.assertIsNone(after.tiers[_CRID].terminal_kind)

    def test_unresolved_unknown_writes_no_terminal(self) -> None:
        path = _journal(self)
        _seed_armed(path, order_id="TR-1", limit=10.0)
        broker = _ResolvingBroker()
        broker.resolutions["TR-1"] = _os("TR-1", OrderStatus.UNKNOWN)  # not_in_retention etc.
        deps = _watch_deps(None, [], broker=broker)

        _run(deps)

        self.assertEqual(
            [ln for ln in _lines(path) if ln["kind"] in entry_trails.ENTRY_TRAIL_TERMINAL_KINDS], []
        )


class TestFlagOffAndNoResolverAreNoops(unittest.TestCase):
    def test_flag_off_does_zero_reconcile_work(self) -> None:
        path = _journal(self)
        _seed_armed(path, order_id="TR-1", limit=10.0)
        broker = _ResolvingBroker()
        broker.resolutions["TR-1"] = _os(
            "TR-1", OrderStatus.FILLED, filled_quantity=100.0, avg_fill_price=10.05
        )
        deps = _watch_deps(None, [], broker=broker)

        _run(deps, env={})  # flag unset

        self.assertEqual([ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_FIRED], [])
        self.assertEqual(broker.resolve_calls, [])

    def test_broker_without_resolution_capability_is_a_noop(self) -> None:
        path = _journal(self)
        _seed_armed(path, order_id="TR-1", limit=10.0)
        broker = _RecordingBroker()  # NOT SupportsOrderResolution
        deps = _watch_deps(None, [], broker=broker)

        _run(deps)  # must not crash, must write nothing

        self.assertEqual([ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_FIRED], [])


if __name__ == "__main__":
    unittest.main()

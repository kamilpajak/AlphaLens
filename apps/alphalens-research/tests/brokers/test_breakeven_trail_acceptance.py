"""End-to-end hermetic acceptance for the ``breakeven_trail`` policy through the
REAL daemon tick entrypoint ``run_once`` — the first policy with
``applies_geometry=False AND trails=True``, a combination that did not exist
before, so nothing in the older suites proves a NO-GEOMETRY policy actually
trails through the whole path (placement drain, peak fetch,
``build_protection_view`` injection, ``reconcile_protection``, the executor
amend, the ``trailed`` marker).

Harness mirrors ``test_trailing_acceptance.py`` byte-for-byte where possible;
only the policy and the pinned numbers differ. Lens arithmetic (the
``be_0p5r_trail0p6`` contract): avg_price=100, brief disaster stop 90 ->
1R = 10; the arm goes live once the peak reaches 105 (entry + 0.5R); the
target is ``100 + 0.6*(peak - 100)`` — NOT an ATR offset, and the stamped
atr=4.0 must be irrelevant to every number below.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import os
import unittest
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from broker_contract.contract import (
    BrokerCapabilityError,
    InstrumentRef,
    OrderState,
    OrderStatus,
    PlacedOrder,
    Position,
)
from broker_contract.exit_geometry import resolve_exit_policy

_UIC = 43070

_BE_TRAIL = resolve_exit_policy("breakeven_trail")


def _instrument(uic: int = _UIC) -> InstrumentRef:
    return InstrumentRef(
        ticker="BIO",
        exchange_mic="XNYS",
        asset_type="Stock",
        broker_instrument_id=str(uic),
        broker_symbol="BIO:xnys",
    )


def _pos(qty: float = 7.0, *, avg_price: float = 100.0, uic: int = _UIC) -> Position:
    return Position(
        instrument=_instrument(uic),
        quantity=qty,
        avg_price=avg_price,
        market_value=None,
        unrealized_pnl=None,
        position_id="pos-1",
    )


def _stop_leg(amount: float = 7.0, *, uic: int = _UIC) -> OrderState:
    return OrderState(
        order_id="stop-1",
        status=OrderStatus.WORKING,
        instrument=None,
        filled_quantity=0.0,
        raw_status="Working",
        uic=uic,
        side="SELL",
        order_type="StopIfTraded",
        amount=amount,
        external_reference="stop-1",
    )


class _Broker:
    """Fake broker exposing the protection reads + place/amend/cancel rails
    (structurally SupportsStandaloneStop + SupportsAmendStop)."""

    name = "betrail-fake"

    def __init__(
        self, *, positions: list[Position], sells: list[OrderState], by_uic: dict[int, Position]
    ) -> None:
        self._positions = positions
        self._sells = sells
        self._by_uic = by_uic
        self.placed: list[tuple[int, str, float, float, str | None]] = []
        # (uic, order_id, side, order_type, new_qty, stop_price, request_id)
        self.amended: list[tuple[int, str, str, str, float, float, str]] = []
        self.cancelled: list[str] = []

    def get_positions(self) -> list[Position]:
        return list(self._positions)

    def get_long_positions(self) -> list[Position]:
        return [p for p in self._positions if p.quantity > 0.5]

    def list_working_sell_orders(self) -> list[OrderState]:
        return list(self._sells)

    def get_positions_by_uic(self, uic: int) -> Position:
        return self._by_uic.get(uic, _pos(0.0, uic=uic))

    def place_standalone_stop(
        self, uic: int, side: str, qty: float, stop_price: float, request_id: str | None = None
    ) -> PlacedOrder:
        self.placed.append((uic, side, qty, stop_price, request_id))
        return PlacedOrder(entry_order_id="S-1", exit_order_ids=())

    def amend_stop_amount(
        self,
        uic: int,
        order_id: str,
        side: str,
        order_type: str,
        new_qty: float,
        stop_price: float,
        request_id: str,
    ) -> PlacedOrder:
        self.amended.append((uic, order_id, side, order_type, new_qty, stop_price, request_id))
        return PlacedOrder(entry_order_id="", exit_order_ids=(order_id,))

    def cancel_order(self, order_id: str) -> None:
        self.cancelled.append(order_id)


class _StopOnlyBroker:
    """SupportsStandaloneStop but NOT SupportsAmendStop — the flag-path
    fail-fast fixture (breakeven_trail sets requires_amend_stop=True)."""

    name = "stoponly"

    def place_standalone_stop(
        self, uic: int, side: str, qty: float, stop_price: float, request_id: str | None = None
    ) -> PlacedOrder:
        return PlacedOrder(entry_order_id="S-1", exit_order_ids=())


def _point(uic: int, price: float):
    from broker_contract.price_feed import PricePoint

    return PricePoint(
        uic=uic,
        bid=price,
        ask=price,
        event_time=dt.datetime(2026, 8, 27, tzinfo=dt.UTC),
        received_at=dt.datetime(2026, 8, 27, tzinfo=dt.UTC),
        source="test",
    )


class _FakeFeed:
    def __init__(self, prices: dict[int, float | None]) -> None:
        self._prices = prices

    def latest(self, uic: int):
        px = self._prices.get(uic)
        return None if px is None else _point(uic, px)


class _ScriptedFeedFactory:
    """One ``_FakeFeed`` per price-consuming call (see
    test_trailing_acceptance.py for the empty-mapping scope-release rule)."""

    def __init__(self, ticks: list[dict[int, float | None]]) -> None:
        self._ticks = list(ticks)
        self.calls = 0

    def __call__(
        self, uic_to_instrument: Mapping[int, tuple[str, str]], *, scope: str
    ) -> _FakeFeed:
        if not uic_to_instrument:
            return _FakeFeed({})
        self.calls += 1
        return _FakeFeed(self._ticks.pop(0))


def _seed_planned(journal: Path) -> None:
    """A ``planned`` line with the geometry shadow stamp so ``plan.reanchor``
    is non-None (the trail arm's guard requires a finite atr even though this
    policy never reads it) — brief disaster floor 90, avg 100."""
    with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
        cl._append_standalone_stop_journal(
            cl._build_planned_line(
                entry_crid="crid-0",
                uic=_UIC,
                side="SELL",
                stop_price=90.0,
                take_profit=None,
                tier_index=0,
                geometry_stamp={"k_atr": 2.0, "atr": 4.0},
            )
        )


def _deps(
    broker: _Broker,
    *,
    exit_policy: object,
    feed_factory: object,
    sink: list[str],
    peak_tracker: dict[int, float] | None = None,
) -> cl.LoopDeps:
    throttle = cl._AlertThrottle(sink.append)
    return cl.LoopDeps(
        broker=broker,  # type: ignore[arg-type]
        kill_file=Path("/nonexistent/KILL"),
        ensure_alive=lambda: type("C", (), {"alive": True, "reason": None})(),  # noqa: PLW0108
        iter_picks=lambda: iter(()),
        place_pick=lambda pick: False,
        read_records=list,
        verdicts_fn=lambda records, broker: [],
        build_position_view=lambda broker, records: cl.BrokerView(working_children={}),
        build_protection_view=functools.partial(cl.build_protection_view, exit_policy=exit_policy),
        execute_protection=cl._make_protection_executor(
            broker,  # type: ignore[arg-type]
            throttle,
            amend_stop=broker.amend_stop_amount,
        ),
        sweep_orphans_fn=lambda broker: [],
        alert=sink.append,
        alert_throttled=lambda msg, reason: bool(sink.append(msg)) or True,
        exit_policy=exit_policy,  # type: ignore[arg-type]
        live_exits_feed_factory=feed_factory,  # type: ignore[arg-type]
        peak_tracker={} if peak_tracker is None else peak_tracker,
    )


def _markers(journal: Path, kind: str) -> list[dict]:
    if not journal.exists():
        return []
    out: list[dict] = []
    for line in journal.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec.get("kind") == kind:
            out.append(rec)
    return out


class TestNoGeometryPolicyTrailsThroughRunOnce(unittest.TestCase):
    """The new combination: ``applies_geometry=False`` (brief exits placed) AND
    ``trails=True`` — a rising feed produces strictly-increasing amends at the
    FRACTIONAL-GIVEBACK levels, and every number is independent of the stamped
    atr (4.0): with an ATR-based risk the first two ticks would not even arm
    the same way, and a Chandelier target would read peak-8, not these."""

    def test_three_rising_ticks_produce_three_fractional_giveback_amends(self) -> None:
        broker = _Broker(positions=[_pos()], sells=[_stop_leg()], by_uic={_UIC: _pos()})
        # peak=110 -> armed (>=105); target 100+0.6*10=106 (floor 109.78 no bind)
        # peak=120 -> target 100+0.6*20=112
        # peak=130 -> target 100+0.6*30=118  (Chandelier would say 122!)
        feed = _ScriptedFeedFactory([{_UIC: 110.0}, {_UIC: 120.0}, {_UIC: 130.0}])
        sink: list[str] = []
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            _seed_planned(journal)
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                deps = _deps(broker, exit_policy=_BE_TRAIL, feed_factory=feed, sink=sink)
                for _ in range(3):
                    cl.run_once(deps)
                trailed = _markers(journal, "trailed")

        self.assertEqual(feed.calls, 3)
        placed_levels = [amend[5] for amend in broker.amended]
        self.assertEqual(len(placed_levels), 3)
        self.assertAlmostEqual(placed_levels[0], 106.0)
        self.assertAlmostEqual(placed_levels[1], 112.0)
        self.assertAlmostEqual(placed_levels[2], 118.0)
        self.assertEqual([m["level"] for m in trailed], placed_levels)
        self.assertEqual(_markers(journal, "reanchored"), [])

    def test_below_the_half_r_arm_the_policy_stays_dark(self) -> None:
        broker = _Broker(positions=[_pos()], sells=[_stop_leg()], by_uic={_UIC: _pos()})
        # peaks 102 and 104.9 both sit below entry + 0.5R = 105 -> no amend.
        # (The ATR-based trailing_atr arm at 103.0 would have fired on 104.9.)
        feed = _ScriptedFeedFactory([{_UIC: 102.0}, {_UIC: 104.9}])
        sink: list[str] = []
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            _seed_planned(journal)
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                deps = _deps(broker, exit_policy=_BE_TRAIL, feed_factory=feed, sink=sink)
                cl.run_once(deps)
                cl.run_once(deps)
                trailed = _markers(journal, "trailed")
        self.assertEqual(broker.amended, [])
        self.assertEqual(trailed, [])

    def test_pullback_after_arming_fires_no_new_amend(self) -> None:
        broker = _Broker(positions=[_pos()], sells=[_stop_leg()], by_uic={_UIC: _pos()})
        # Arm at 120 (target 112), then pull back to 108: the peak (and so the
        # target) never retreats; the ratchet drops the unchanged proposal.
        feed = _ScriptedFeedFactory([{_UIC: 120.0}, {_UIC: 108.0}])
        sink: list[str] = []
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            _seed_planned(journal)
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                deps = _deps(broker, exit_policy=_BE_TRAIL, feed_factory=feed, sink=sink)
                cl.run_once(deps)
                cl.run_once(deps)
        self.assertEqual(len(broker.amended), 1)
        self.assertAlmostEqual(broker.amended[0][5], 112.0)

    def test_restart_does_not_loosen_and_resumes_on_a_new_high(self) -> None:
        broker = _Broker(positions=[_pos()], sells=[_stop_leg()], by_uic={_UIC: _pos()})
        sink: list[str] = []
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            _seed_planned(journal)
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                # Session 1: arm at 120 -> amend to 112, journaled.
                feed1 = _ScriptedFeedFactory([{_UIC: 120.0}])
                deps1 = _deps(broker, exit_policy=_BE_TRAIL, feed_factory=feed1, sink=sink)
                cl.run_once(deps1)
                self.assertEqual(len(broker.amended), 1)

                # "Restart": empty peak_tracker. Tick A at 108 sits ABOVE the
                # 0.5R arm (105) so the policy proposes 100+0.6*8=104.8 — a
                # LOOSEN vs the journaled 112 floor -> dropped. Tick B at 125
                # clears the pre-restart high -> target 115 -> one re-raise.
                feed2 = _ScriptedFeedFactory([{_UIC: 108.0}, {_UIC: 125.0}])
                deps2 = _deps(broker, exit_policy=_BE_TRAIL, feed_factory=feed2, sink=sink)
                cl.run_once(deps2)
                self.assertEqual(len(broker.amended), 1, "must not loosen post-restart")
                cl.run_once(deps2)
                self.assertEqual(len(broker.amended), 2)
                self.assertAlmostEqual(broker.amended[1][5], 115.0)


_BE_TRAIL_FLAG = {"ALPHALENS_BROKER_EXIT_POLICY": "breakeven_trail"}


class TestBuildDefaultDepsFlagPath(unittest.TestCase):
    """``ALPHALENS_BROKER_EXIT_POLICY=breakeven_trail`` through the REAL
    ``build_default_deps``: a capable (SupportsAmendStop) broker resolves the
    policy onto deps; an incapable one fail-fasts (requires_amend_stop=True)."""

    def test_capable_broker_resolves_breakeven_trail_onto_deps(self) -> None:
        capable = _Broker(positions=[], sells=[], by_uic={})
        with (
            TemporaryDirectory() as home_dir,
            mock.patch("pathlib.Path.home", return_value=Path(home_dir)),
            mock.patch(
                "alphalens_pipeline.brokers.registry.get_default_broker",
                return_value=capable,
            ),
            mock.patch.object(cl, "_default_oauth_provider", return_value=mock.Mock()),
            mock.patch.dict(os.environ, _BE_TRAIL_FLAG),
        ):
            deps = cl.build_default_deps(
                notify=lambda _msg: None, chain_loss_notify=lambda _msg: None
            )
        self.assertEqual(deps.exit_policy.name, "breakeven_trail")
        self.assertIsNone(deps.exit_policy.geometry_name)
        self.assertTrue(deps.exit_policy.trails)
        self.assertFalse(deps.exit_policy.applies_geometry)

    def test_incapable_broker_fails_fast(self) -> None:
        incapable = _StopOnlyBroker()
        with (
            TemporaryDirectory() as home_dir,
            mock.patch("pathlib.Path.home", return_value=Path(home_dir)),
            mock.patch(
                "alphalens_pipeline.brokers.registry.get_default_broker",
                return_value=incapable,
            ),
            mock.patch.object(cl, "_default_oauth_provider", return_value=mock.Mock()),
            mock.patch.dict(os.environ, _BE_TRAIL_FLAG),
        ):
            with self.assertRaises(BrokerCapabilityError):
                cl.build_default_deps(notify=lambda _msg: None, chain_loss_notify=lambda _msg: None)


if __name__ == "__main__":
    unittest.main()

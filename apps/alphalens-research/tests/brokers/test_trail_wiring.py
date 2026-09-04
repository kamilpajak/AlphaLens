"""Task 4: wire the high-water peaks into the protection pass + persist the
``trailed`` journal marker on a confirmed trail amend.

These are END-TO-END tests of ``_run_protection_pass`` (not the pure
``_maybe_trail`` arm — that lives in ``automanager/test_maybe_trail.py``): a fake
broker + a scripted price feed drive a real protection pass so the peak fetch,
the ``build_protection_view`` injection, the reconcile, the executor amend, and
the ``trailed`` marker write are all exercised together.

Safety invariants under test:
  * trailing ON + rising feed -> an ``AmendStop(reason="trail")`` is executed AND
    a ``trailed`` marker (with the clamped level + peak/last_price telemetry) is
    appended;
  * the default policy (``atr_bracket_1p5``, ``trails=False``) NEVER touches the
    feed factory -> byte-identical to today, no ``trailed`` marker;
  * a ``trailed`` marker on tick N folds into ``trailed_stop_by_uic`` and vetoes a
    non-stepping proposal on tick N+1 (the cross-tick ratchet);
  * CARRYOVER-1: a pullback whose PRE-clamp proposal clears the ratchet floor but
    whose POST-clamp level would land BELOW the trail history is dropped;
  * CARRYOVER-2: a peak-fetch failure degrades trailing to dark this tick but the
    never-naked reconcile STILL runs (the naked long is covered by a PlaceStop).
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager.position_manager import (
    AmendStop,
    PlannedExit,
    ProtectionView,
    ReanchorFacts,
    _maybe_trail,
)
from broker_contract.contract import (
    InstrumentRef,
    OrderState,
    OrderStatus,
    PlacedOrder,
    Position,
)
from broker_contract.exit_geometry import resolve_exit_policy
from broker_contract.exit_geometry.policy import TrailingAtrPolicy
from broker_contract.exit_geometry.registry import resolve_policy
from broker_contract.price_feed import PricePoint

_UIC = 43070

# atr_bracket_1p5 base (stop_atr_mult=1.5): activation fires at
# ``peak >= avg_price + activation_r*1.5*atr``; the Chandelier target is
# ``peak - k_atr*atr``.
_TRAIL = TrailingAtrPolicy(
    resolve_policy("atr_bracket_1p5"), name="trailing_atr", activation_r=0.5, k_atr=2.0
)
_ATR_BRACKET = resolve_exit_policy("atr_bracket_1p5")  # trails=False sibling


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
    """Fake broker exposing the protection reads + place/amend/cancel rails."""

    name = "trail-fake"

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


def _point(uic: int, price: float) -> PricePoint:
    return PricePoint(
        uic=uic,
        bid=price,
        ask=price,
        event_time=dt.datetime(2026, 8, 9, tzinfo=dt.UTC),
        received_at=dt.datetime(2026, 8, 9, tzinfo=dt.UTC),
        source="test",
    )


class _FakeFeed:
    def __init__(self, prices: dict[int, float | None]) -> None:
        self._prices = prices

    def latest(self, uic: int) -> PricePoint | None:
        px = self._prices.get(uic)
        return None if px is None else _point(uic, px)


class _ScriptedFeedFactory:
    """One ``_FakeFeed`` per call, popped off a per-tick script; ``calls`` counts
    invocations so a default-policy test can assert the feed is NEVER touched."""

    def __init__(self, ticks: list[dict[int, float | None]]) -> None:
        self._ticks = list(ticks)
        self.calls = 0

    def __call__(self, _uic_to_instrument: object, *, scope: str) -> _FakeFeed:
        self.calls += 1
        return _FakeFeed(self._ticks.pop(0))


class _RaisingFeedFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _uic_to_instrument: object, *, scope: str) -> _FakeFeed:
        self.calls += 1
        raise RuntimeError("simulated feed/network/auth failure")


def _seed_planned(journal: Path, *, take_profit: float | None = None) -> None:
    """A `planned` line WITH the geometry shadow stamp so ``plan.reanchor`` is
    non-None (both the trail and reanchor arms require it)."""
    with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
        cl._append_standalone_stop_journal(
            cl._build_planned_line(
                entry_crid="crid-0",
                uic=_UIC,
                side="SELL",
                stop_price=90.0,
                take_profit=take_profit,
                tier_index=0,
                geometry_stamp={"k_atr": 2.0, "atr": 4.0},
            )
        )


def _deps(
    broker: _Broker, *, exit_policy: object, feed_factory: object, sink: list[str]
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


class TestTrailingPathEmitsAmendAndTrailedMarker(unittest.TestCase):
    """Trailing policy ON + a rising feed -> the protection pass amends the stop UP
    and persists a ``trailed`` marker carrying the clamped level + telemetry."""

    def test_amend_and_marker_written(self) -> None:
        broker = _Broker(positions=[_pos()], sells=[_stop_leg()], by_uic={_UIC: _pos()})
        feed = _ScriptedFeedFactory([{_UIC: 104.0}])
        sink: list[str] = []
        report = cl.TickReport()
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            _seed_planned(journal)
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                deps = _deps(broker, exit_policy=_TRAIL, feed_factory=feed, sink=sink)
                cl._run_protection_pass(deps, [], False, report)
                trailed = _markers(journal, "trailed")

        self.assertEqual(feed.calls, 1)  # trailing path fetched the feed once
        self.assertEqual(len(broker.amended), 1)
        # target 104 - k_atr 2 * atr 4 = 96.0 (live-price clamp floor 103.79 > 96)
        self.assertAlmostEqual(broker.amended[0][5], 96.0)  # stop_price placed
        self.assertIn(("protection", "AmendStop"), report.actions)

        self.assertEqual(len(trailed), 1)
        self.assertEqual(trailed[0]["uic"], _UIC)
        self.assertAlmostEqual(trailed[0]["level"], 96.0)  # the clamped level placed
        self.assertAlmostEqual(trailed[0]["peak"], 104.0)  # telemetry substrate
        self.assertAlmostEqual(trailed[0]["last_price"], 104.0)
        # a trail must NEVER be recorded as a reanchor
        self.assertEqual(_markers(journal, "reanchored"), [])


class TestDefaultPolicyNeverFetchesFeed(unittest.TestCase):
    """The default (non-trailing) policy takes the exact 2-arg build call with NO
    peak fetch: the feed factory is never invoked and no ``trailed`` marker is
    written -> byte-identical to today."""

    def test_atr_bracket_never_touches_the_feed(self) -> None:
        broker = _Broker(positions=[_pos()], sells=[_stop_leg()], by_uic={_UIC: _pos()})
        feed = _ScriptedFeedFactory([{_UIC: 104.0}])  # would raise IndexError if popped
        sink: list[str] = []
        report = cl.TickReport()
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            _seed_planned(journal)
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                deps = _deps(broker, exit_policy=_ATR_BRACKET, feed_factory=feed, sink=sink)
                cl._run_protection_pass(deps, [], False, report)
                trailed = _markers(journal, "trailed")

        self.assertEqual(feed.calls, 0)  # the non-trailing path NEVER fetches
        self.assertEqual(trailed, [])  # and never journals a trailed marker


class TestCrossTickRatchet(unittest.TestCase):
    """A ``trailed`` marker on tick N folds into ``trailed_stop_by_uic`` and vetoes
    a non-stepping proposal on tick N+1 -> exactly one amend across two ticks."""

    def test_second_tick_non_stepping_proposal_is_vetoed(self) -> None:
        broker = _Broker(positions=[_pos()], sells=[_stop_leg()], by_uic={_UIC: _pos()})
        # Same price both ticks -> peak stays 104 -> proposal stays 96 -> tick 2's
        # 96 does not clear the folded floor 96 by _TRAIL_STEP_EPS -> dropped.
        feed = _ScriptedFeedFactory([{_UIC: 104.0}, {_UIC: 104.0}])
        sink: list[str] = []
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            _seed_planned(journal)
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                deps = _deps(broker, exit_policy=_TRAIL, feed_factory=feed, sink=sink)
                cl._run_protection_pass(deps, [], False, cl.TickReport())
                cl._run_protection_pass(deps, [], False, cl.TickReport())
                trailed = _markers(journal, "trailed")

        self.assertEqual(len(broker.amended), 1)  # tick 2 vetoed by the ratchet
        self.assertEqual(len(trailed), 1)


class TestRatchetSurvivesDaemonRestart(unittest.TestCase):
    """#1324: the ratchet floor must survive a daemon restart. A restart does two
    things — it re-seeds ``deps.peak_tracker`` from the live price (deliberate),
    and it runs ``_compact_standalone_stop_journal()`` over the journal. The
    compactor used to drop every ``trailed`` marker, so the floor went with it
    and the next tick was free to PATCH the stop BELOW the level it already
    stood at.

    Both arms are identical except for the compaction call, so the compactor —
    not the fresh ``LoopDeps`` — is the discriminator."""

    def _two_ticks(self, *, compact_between: bool) -> tuple[list, list[dict]]:
        broker = _Broker(positions=[_pos()], sells=[_stop_leg()], by_uic={_UIC: _pos()})
        sink: list[str] = []
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            _seed_planned(journal)
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                # Tick 1: peak 104 -> trail the stop to 104 - 2*4 = 96.0.
                deps = _deps(
                    broker,
                    exit_policy=_TRAIL,
                    feed_factory=_ScriptedFeedFactory([{_UIC: 104.0}]),
                    sink=sink,
                )
                cl._run_protection_pass(deps, [], False, cl.TickReport())

                if compact_between:
                    cl._compact_standalone_stop_journal()  # the daemon's boot step

                # Tick 2 on a FRESH LoopDeps == the restarted daemon: the peak
                # tracker re-seeds from 103.5, so the proposal drops to 95.5.
                # Only the journal's trailed marker can veto it.
                restarted = _deps(
                    broker,
                    exit_policy=_TRAIL,
                    feed_factory=_ScriptedFeedFactory([{_UIC: 103.5}]),
                    sink=sink,
                )
                cl._run_protection_pass(restarted, [], False, cl.TickReport())
                return broker.amended, _markers(journal, "trailed")

    def test_control_no_compaction_keeps_the_ratchet(self) -> None:
        # Control arm: fresh deps alone (peak tracker reset) do NOT loosen the
        # stop — so a second amend in the other arm is caused by the compaction.
        amended, trailed = self._two_ticks(compact_between=False)
        self.assertEqual([round(a[5], 4) for a in amended], [96.0])
        self.assertEqual([round(m["level"], 4) for m in trailed], [96.0])

    def test_boot_compaction_does_not_loosen_the_trailed_stop(self) -> None:
        amended, trailed = self._two_ticks(compact_between=True)
        self.assertEqual(
            [round(a[5], 4) for a in amended],
            [96.0],
            "the post-restart tick must stay vetoed by the folded ratchet floor",
        )
        self.assertEqual([round(m["level"], 4) for m in trailed], [96.0])


class TestCarryover1PullbackLoosenDropped(unittest.TestCase):
    """CARRYOVER-1 (pure arm): a pullback whose PRE-clamp proposal clears the
    ratchet floor but whose POST-clamp level would land BELOW the trail history is
    dropped. Gating on the raw ``proposed`` (the pre-fix bug) would emit a stop
    below the prior trailed level (a loosen vs live trail history)."""

    def test_clamped_below_floor_is_dropped_though_proposed_clears_it(self) -> None:
        pos = _pos(avg_price=100.0)  # qty 7
        plan = PlannedExit(
            uic=_UIC,
            entry_crid="crid",
            side="SELL",
            stop_price=90.0,  # brief floor well below everything here
            tp_price=None,
            conflicting=False,
            n_plans=1,
            reanchor=ReanchorFacts(k_atr=2.0, atr=4.0),
        )
        legs = (_stop_leg(),)
        # peak 120 -> proposed 120 - 2*4 = 112 (clears floor 100.0 + eps easily).
        # live price 100 (pulled back) -> clamp floor 100*0.998 = 99.8 -> the placed
        # level would be 99.8, BELOW the prior trailed 100.0 = a loosen. The fix
        # gates on the clamped 99.8 (<= 100.0 + eps) -> dropped.
        view = ProtectionView(
            long_positions={_UIC: pos},
            all_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: legs},
            planned_by_uic={_UIC: plan},
            oco_unsupported=frozenset(),
            exit_policy=_TRAIL,
            peak_by_uic={_UIC: 120.0},
            last_price_by_uic={_UIC: 100.0},
            trailed_stop_by_uic={_UIC: 100.0},  # prior CONFIRMED trailed level
        )
        self.assertIsNone(_maybe_trail(_UIC, pos, plan, legs, view))

    def test_a_genuine_step_up_still_fires(self) -> None:
        # Control: same shape but the clamp does NOT bind (live price high), so the
        # placed level clears the floor and the amend fires -> proves the drop above
        # is the clamp/ratchet interaction, not a blanket veto.
        pos = _pos(avg_price=100.0)
        plan = PlannedExit(
            uic=_UIC,
            entry_crid="crid",
            side="SELL",
            stop_price=90.0,
            tp_price=None,
            conflicting=False,
            n_plans=1,
            reanchor=ReanchorFacts(k_atr=2.0, atr=4.0),
        )
        legs = (_stop_leg(),)
        # peak 112, live 112 -> clamp floor 111.78 does not bind -> clamped 104.0,
        # clears the prior trailed floor 100.0 by well over eps -> fires.
        view = ProtectionView(
            long_positions={_UIC: pos},
            all_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: legs},
            planned_by_uic={_UIC: plan},
            oco_unsupported=frozenset(),
            exit_policy=_TRAIL,
            peak_by_uic={_UIC: 112.0},
            last_price_by_uic={_UIC: 112.0},
            trailed_stop_by_uic={_UIC: 100.0},
        )
        action = _maybe_trail(_UIC, pos, plan, legs, view)
        self.assertIsInstance(action, AmendStop)
        assert isinstance(action, AmendStop)
        self.assertAlmostEqual(action.stop_price, 104.0)


class TestCarryover2FeedFailureLeavesNeverNakedIntact(unittest.TestCase):
    """CARRYOVER-2: a peak-fetch failure alerts + degrades trailing to dark this
    tick, but the never-naked reconcile STILL runs -> a naked long is covered by a
    PlaceStop with empty peak maps."""

    def test_feed_raise_still_covers_the_naked_long(self) -> None:
        # A naked long (NO covering sell leg): reconcile must emit a PlaceStop.
        broker = _Broker(positions=[_pos()], sells=[], by_uic={_UIC: _pos()})
        feed = _RaisingFeedFactory()
        sink: list[str] = []
        report = cl.TickReport()
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            _seed_planned(journal, take_profit=None)  # no TP -> plain standalone stop
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                deps = _deps(broker, exit_policy=_TRAIL, feed_factory=feed, sink=sink)
                cl._run_protection_pass(deps, [], False, report)  # must not raise
                trailed = _markers(journal, "trailed")

        self.assertEqual(feed.calls, 1)  # the fetch was attempted
        self.assertEqual(len(broker.placed), 1)  # never-naked backstop still ran
        self.assertAlmostEqual(broker.placed[0][3], 90.0)  # at the brief floor
        self.assertEqual(broker.amended, [])  # trailing dark this tick
        self.assertEqual(trailed, [])  # no trail marker on a dark tick
        self.assertTrue(
            any("peak fetch failed" in msg for msg in sink),
            "the peak-fetch failure must be surfaced via a throttled alert",
        )


class TestPeakFetchCapabilityGuard(unittest.TestCase):
    """#1141: a broker without the netted position reads is refused EXPLICITLY
    before the peak fetch — a distinct alert, not the generic except-Exception
    "peak fetch failed" path. Production cannot reach this (the boot gate
    refuses such a broker), so this pins the defensive stance for direct /
    test-composed deps."""

    def test_broker_without_reads_goes_dark_with_a_distinct_alert(self) -> None:
        class _NoReadsBroker:
            name = "noreads"

            def amend_stop_amount(self, *args: object, **kwargs: object) -> None:
                raise AssertionError("never called")

        sink: list[str] = []
        deps = _deps(
            _NoReadsBroker(),  # type: ignore[arg-type]
            exit_policy=_TRAIL,
            feed_factory=_ScriptedFeedFactory([]),
            sink=sink,
        )
        report = cl.TickReport()
        peaks, last = cl._fetch_protection_peaks(deps, report)
        self.assertEqual((peaks, last), ({}, {}))
        self.assertTrue(
            any("lacks netted position reads" in msg for msg in sink),
            f"expected the capability alert, got: {sink}",
        )


if __name__ == "__main__":
    unittest.main()

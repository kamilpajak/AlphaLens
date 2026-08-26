"""Task 5: end-to-end hermetic acceptance for the ``trailing_atr`` bot-amend
trailing stop, proven through the REAL daemon tick entrypoint ``run_once`` (not
the lower-level ``_run_protection_pass`` the Task 4 wiring tests use) so the
whole path — placement drain, verdict advance, the (dark) live-exits pass, the
peak fetch, ``build_protection_view`` injection, ``reconcile_protection``, the
executor amend, and the ``trailed`` journal marker — is exercised together.

Also proves the ``ALPHALENS_BROKER_EXIT_POLICY=trailing_atr`` env flag path
through ``build_default_deps`` (control_loop.py ~1119): a capable
(``SupportsAmendStop``) broker resolves ``trailing_atr`` onto ``LoopDeps``, and
an incapable broker FAIL-FASTS via the existing ``requires_amend_stop`` gate
(control_loop.py ~1156) — Task 1 already registered ``trailing_atr`` with
``requires_amend_stop=True``, so no new production code is expected here; this
test only proves the wiring.

Harness mirrors ``tests/brokers/test_trail_wiring.py`` (Task 4): a fake broker
exposing the protection reads + place/amend rails, a scripted ``PriceFeed``
factory injected via ``LoopDeps.live_exits_feed_factory``, and a real temp-file
standalone-stop journal read/written through the real
``_standalone_stop_journal_path`` module function (patched per-test). The
``TestBuildDefaultDepsFlagPath`` class additionally mirrors
``tests/brokers/automanager/test_control_loop.py::TestBuildDefaultDepsExitPolicyCapabilityGate``
for the ``build_default_deps`` seam (mocking ``get_default_broker`` +
``_default_oauth_provider``).
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
from broker_contract.exit_geometry.policy import TrailingAtrPolicy
from broker_contract.exit_geometry.registry import resolve_policy
from broker_contract.price_feed import PricePoint

_UIC = 43070

# activation_r=0.5, k_atr=2.0 over the atr_bracket_1p5 base (stop_atr_mult=1.5):
# avg_price=100, atr=4 -> risk=1.5*4=6 -> activation fires once peak clears
# 100 + 0.5*6 = 103. Chandelier target = peak - 2*atr. min_stop_distance_frac
# (inherited from atr_bracket_1p5) = 0.002 -> the live-price clamp floor is
# last_price*0.998, matching test_trail_wiring.py's pinned numbers.
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
    """Fake broker exposing the protection reads + place/amend/cancel rails —
    structurally satisfies both ``SupportsStandaloneStop`` and
    ``SupportsAmendStop`` (both are ``runtime_checkable`` Protocols keyed off
    method presence only)."""

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


class _StopOnlyBroker:
    """SupportsStandaloneStop but NOT SupportsAmendStop (no amend_stop_amount) —
    the flag-path fail-fast fixture."""

    name = "stoponly"

    def place_standalone_stop(
        self, uic: int, side: str, qty: float, stop_price: float, request_id: str | None = None
    ) -> PlacedOrder:
        return PlacedOrder(entry_order_id="S-1", exit_order_ids=())


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
    """One ``_FakeFeed`` per PRICE-CONSUMING call, popped off a per-tick script;
    ``calls`` counts those so the default-policy test can assert the feed is
    NEVER touched. A call with an EMPTY uic mapping is a scope RELEASE (a quiet
    pass handing its slice of the shared subscription an empty set — see
    ``_release_feed_scope``): it has no price consumer, so it neither burns a
    scripted tick nor counts as a fetch."""

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
    """A ``planned`` line WITH the geometry shadow stamp so ``plan.reanchor`` is
    non-None (the trail arm requires it) — avg_price 100, atr 4 (matches _TRAIL's
    pinned constants above), brief disaster floor 90."""
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
    """Mirrors test_trail_wiring.py's ``_deps`` — the real ``build_protection_view``
    + the real ``_make_protection_executor`` — but adds every field ``run_once``
    itself needs (``place_pick``/``verdicts_fn``/``sweep_orphans_fn``/...), all
    wired to inert no-ops so a tick reduces to the KILL-alert edge check + the
    (empty) placement/verdict/live-exits passes + the real protection pass."""
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


class TestRisingPathStepsUpTheStopThroughRunOnce(unittest.TestCase):
    """A covered long under ``trailing_atr``, driven by a rising feed across
    several ``run_once`` ticks: the stop is amended UP in coarse steps, never
    down, and each confirmed amend leaves a ``trailed`` marker."""

    def test_three_rising_ticks_produce_three_strictly_increasing_amends(self) -> None:
        broker = _Broker(positions=[_pos()], sells=[_stop_leg()], by_uic={_UIC: _pos()})
        # peak=110 -> target 110-8=102 (floor 109.78 doesn't bind)
        # peak=120 -> target 120-8=112 (floor 119.76 doesn't bind)
        # peak=130 -> target 130-8=122 (floor 129.74 doesn't bind)
        feed = _ScriptedFeedFactory([{_UIC: 110.0}, {_UIC: 120.0}, {_UIC: 130.0}])
        sink: list[str] = []
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            _seed_planned(journal)
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                deps = _deps(broker, exit_policy=_TRAIL, feed_factory=feed, sink=sink)
                reports = [cl.run_once(deps) for _ in range(3)]
                trailed = _markers(journal, "trailed")

        self.assertEqual(feed.calls, 3)
        self.assertEqual(len(broker.amended), 3)
        placed_levels = [amend[5] for amend in broker.amended]
        self.assertAlmostEqual(placed_levels[0], 102.0)
        self.assertAlmostEqual(placed_levels[1], 112.0)
        self.assertAlmostEqual(placed_levels[2], 122.0)
        # strictly increasing -> never a down-move across the three ticks.
        self.assertLess(placed_levels[0], placed_levels[1])
        self.assertLess(placed_levels[1], placed_levels[2])
        for report in reports:
            self.assertIn(("protection", "AmendStop"), report.actions)

        self.assertEqual(len(trailed), 3)
        marker_levels = [m["level"] for m in trailed]
        self.assertEqual(marker_levels, placed_levels)
        for marker, peak in zip(trailed, (110.0, 120.0, 130.0), strict=True):
            self.assertAlmostEqual(marker["peak"], peak)
            self.assertAlmostEqual(marker["last_price"], peak)
        # a trail must never be recorded as a one-shot reanchor.
        self.assertEqual(_markers(journal, "reanchored"), [])


class TestFallingPathEmitsNoFurtherAmends(unittest.TestCase):
    """After the stop is armed by a rising feed, a subsequent falling/flat feed
    produces NO further trail amends — the high-water peak (and therefore the
    Chandelier target) never retreats, and the live-price clamp only ever pulls
    the proposal BELOW the ratchet floor, never above it."""

    def test_two_pullback_ticks_after_arming_fire_no_new_amend(self) -> None:
        broker = _Broker(positions=[_pos()], sells=[_stop_leg()], by_uic={_UIC: _pos()})
        # Arm with a single rising tick to 130.0 (target 122.0, marker written),
        # then two pullback ticks: 125.0 (clamp doesn't bind, ratchet drops it)
        # and 115.0 (clamp DOES bind at 114.77, still below the 122 floor).
        feed = _ScriptedFeedFactory([{_UIC: 130.0}, {_UIC: 125.0}, {_UIC: 115.0}])
        sink: list[str] = []
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            _seed_planned(journal)
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                deps = _deps(broker, exit_policy=_TRAIL, feed_factory=feed, sink=sink)
                cl.run_once(deps)  # arm: amend to 122.0
                amends_after_arm = len(broker.amended)
                cl.run_once(deps)  # pullback to 125 -> dropped
                cl.run_once(deps)  # pullback to 115 -> dropped
                trailed = _markers(journal, "trailed")

        self.assertEqual(amends_after_arm, 1)
        self.assertAlmostEqual(broker.amended[0][5], 122.0)
        # No amend added by either pullback tick.
        self.assertEqual(len(broker.amended), 1)
        self.assertEqual(len(trailed), 1)


class TestRestartDoesNotLoosenTheStop(unittest.TestCase):
    """A daemon restart resets the in-memory ``peak_tracker`` to empty (a fresh
    ``LoopDeps``), but the ratchet floor folded from the ON-DISK ``trailed``
    marker still holds — a lower first-observed-post-restart price is dropped,
    not treated as a fresh (lower) high-water mark. Price later re-clearing the
    pre-restart high resumes trailing normally (at most one re-raise)."""

    def test_fresh_peak_tracker_does_not_undo_the_journaled_ratchet(self) -> None:
        broker = _Broker(positions=[_pos()], sells=[_stop_leg()], by_uic={_UIC: _pos()})
        sink: list[str] = []
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            _seed_planned(journal)
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                # Session 1 (pre-restart): one tick at 130.0 -> target 122.0, armed
                # + journaled.
                feed1 = _ScriptedFeedFactory([{_UIC: 130.0}])
                deps1 = _deps(broker, exit_policy=_TRAIL, feed_factory=feed1, sink=sink)
                cl.run_once(deps1)
                self.assertEqual(len(broker.amended), 1)
                self.assertAlmostEqual(broker.amended[0][5], 122.0)

                # "Restart": a BRAND NEW LoopDeps with an EMPTY peak_tracker (the
                # dataclass default), same journal on disk, and a two-tick script:
                # first a PULLBACK vs the true (now-forgotten) 130 peak, then a
                # price that clears the pre-restart high again.
                feed2 = _ScriptedFeedFactory([{_UIC: 125.0}, {_UIC: 140.0}])
                deps2 = _deps(broker, exit_policy=_TRAIL, feed_factory=feed2, sink=sink)
                self.assertEqual(deps2.peak_tracker, {})  # confirms the reset

                cl.run_once(deps2)  # tick A: 125.0 (post-restart pullback)
                # The journal-folded ratchet floor (122.0) still holds: a naive
                # peak=125 -> target 117.0 would be a LOOSEN vs 122.0 and must be
                # dropped, not placed.
                self.assertEqual(len(broker.amended), 1, "must not loosen post-restart")

                cl.run_once(deps2)  # tick B: 140.0 (clears the pre-restart high)
                # Trailing resumes, re-raising the stop exactly once.
                self.assertEqual(len(broker.amended), 2)
                self.assertAlmostEqual(broker.amended[1][5], 132.0)

                trailed = _markers(journal, "trailed")
        self.assertEqual(len(trailed), 2)
        self.assertAlmostEqual(trailed[0]["level"], 122.0)
        self.assertAlmostEqual(trailed[1]["level"], 132.0)


class TestDefaultPolicyIsByteIdenticalThroughRunOnce(unittest.TestCase):
    """The SAME rising-feed scenario under the default ``atr_bracket_1p5`` policy
    (``trails=False``) never touches the feed factory and never writes a
    ``trailed`` marker — byte-identical to today, proven through the real
    ``run_once`` entrypoint (not just the lower-level protection pass)."""

    def test_atr_bracket_never_fetches_the_feed_or_writes_a_trailed_marker(self) -> None:
        broker = _Broker(positions=[_pos()], sells=[_stop_leg()], by_uic={_UIC: _pos()})
        # Would raise IndexError if popped -> proves the feed is never even called.
        feed = _ScriptedFeedFactory([{_UIC: 110.0}, {_UIC: 120.0}, {_UIC: 130.0}])
        sink: list[str] = []
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            _seed_planned(journal)
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                deps = _deps(broker, exit_policy=_ATR_BRACKET, feed_factory=feed, sink=sink)
                for _ in range(3):
                    cl.run_once(deps)
                trailed = _markers(journal, "trailed")

        self.assertEqual(feed.calls, 0)
        self.assertEqual(trailed, [])


_AMEND_ON = {"ALPHALENS_BROKER_AMEND_ENABLED": "1"}
_TRAILING_FLAG = {"ALPHALENS_BROKER_EXIT_POLICY": "trailing_atr"}


class TestBuildDefaultDepsFlagPath(unittest.TestCase):
    """The ``ALPHALENS_BROKER_EXIT_POLICY=trailing_atr`` env flag path through
    the REAL ``build_default_deps`` (control_loop.py ~1119/~1156) — mirrors
    ``test_control_loop.py::TestBuildDefaultDepsExitPolicyCapabilityGate``. No
    new production code: Task 1 already registered ``trailing_atr`` with
    ``requires_amend_stop=True`` in the exit-policy registry, and the capability
    fail-fast already existed before this task — this proves both fire for the
    trailing_atr name specifically."""

    def test_capable_broker_resolves_a_trailing_policy_onto_deps(self) -> None:
        capable = _Broker(positions=[], sells=[], by_uic={})
        with (
            TemporaryDirectory() as home_dir,
            mock.patch("pathlib.Path.home", return_value=Path(home_dir)),
            mock.patch(
                "alphalens_pipeline.brokers.registry.get_default_broker",
                return_value=capable,
            ),
            mock.patch.object(cl, "_default_oauth_provider", return_value=mock.Mock()),
            mock.patch.dict(os.environ, _TRAILING_FLAG),
        ):
            deps = cl.build_default_deps(
                notify=lambda _msg: None, chain_loss_notify=lambda _msg: None
            )
        # The flag path resolves the TRAILING policy, and the deps now say so.
        # Before #1138 this asserted "atr_bracket_1p5" — the name of the policy
        # this path deliberately does NOT resolve.
        self.assertEqual(deps.exit_policy.name, "trailing_atr")
        self.assertEqual(deps.exit_policy.geometry_name, "atr_bracket_1p5")
        self.assertTrue(deps.exit_policy.trails)
        self.assertTrue(deps.exit_policy.requires_amend_stop)

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
            mock.patch.dict(os.environ, _TRAILING_FLAG),
        ):
            with self.assertRaises(BrokerCapabilityError):
                cl.build_default_deps(notify=lambda _msg: None, chain_loss_notify=lambda _msg: None)


if __name__ == "__main__":
    unittest.main()

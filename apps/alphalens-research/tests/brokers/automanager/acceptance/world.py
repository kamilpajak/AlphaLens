"""ManagerWorld — the scenario DSL for the acceptance suite.

Every acceptance test reads as a sentence: a few GIVEN calls set the scene, one
WHEN call runs a management tick, and one THEN call checks a guarantee. All the
mechanics the auto-manager actually uses — uic numbers, the plan journal, the
env flags, the alert plumbing — live in here so the tests stay in plain business
language.

PR-8 (broker-manager extraction memo section 5.3): the WHEN seam
(``run_tick``) drives the REAL manager THROUGH the formal
``ManagerService`` Protocol boundary — ``InProcessManagerService`` wraps the
same real ``control_loop.run_once`` (with the real ``build_protection_view``,
the real reconcile, and the real protection executor) against the fake
in-memory broker, but the world's OWN code never calls ``control_loop``
directly for placement or protection any more (``arm``/``run_tick`` go
through ``submit_intent``/``run_cycle``). Nothing about the tick is stubbed
except the inputs a human would set up (positions, plans, faults) — this
proves the six acceptance guarantees hold identically when driven through the
Protocol, not just against ``LoopDeps`` directly (the pre-extraction proof
that the client<->manager boundary is real).
"""

from __future__ import annotations

import datetime as dt
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager import safety, service
from broker_contract.contract import _QTY_EPS, OrderStatus
from broker_contract.price_feed import PricePoint

from .fake_broker import FakeBroker

_DEFAULT_STOP = 44.0
_DEFAULT_TP = 59.0
_DEFAULT_FILL = 50.0


class _Chain:
    """A tiny session-chain status object (what ensure_alive returns)."""

    def __init__(self, *, alive: bool, reason: str = "") -> None:
        self.alive = alive
        self.reason = reason


class _WorldPriceFeed:
    """A ``PriceFeed`` reading the world's own ``price_is``-set prices, keyed by
    uic (INC-5 live-exits scenarios). A uic with no price set reads as stale
    (``None``) — the engine's stream-health veto."""

    def __init__(self, prices: dict[int, float]) -> None:
        self._prices = prices

    def latest(self, uic: int) -> PricePoint | None:
        price = self._prices.get(uic)
        return (
            None
            if price is None
            else PricePoint(
                uic=uic,
                bid=price,
                ask=price,
                event_time=dt.datetime.now(dt.UTC),
                received_at=dt.datetime.now(dt.UTC),
                source="test",
            )
        )


class ManagerWorld:
    """A single self-contained world the manager runs inside.

    Construct one per test: ``world = ManagerWorld(self)``. It registers its own
    cleanup on the test, so tests never manage tempfiles or env vars by hand.
    """

    def __init__(self, test: unittest.TestCase, *, equity: float = 1_000_000.0) -> None:
        self.broker = FakeBroker(equity=equity)
        # The REAL delivery sink — kept fed exactly as before (PR-8 does not
        # change what gets alerted, only how the THEN-side asserts read it).
        self.alerts: list[str] = []
        # What assert_alerted/assert_silent actually read: alert-kind events
        # drained from service.stream_events() after every run_tick (memo
        # section 5.3 — guarantee #5 becomes a direct assertion on the event
        # stream). Accumulates across ticks within one test, mirroring the
        # old self.alerts' never-cleared lifetime.
        self._captured_alert_events: list[str] = []
        # The tee lives at the shared _AlertThrottle's BASE sink, not on the
        # deps.alert_throttled FIELD: the protection executor
        # (_make_protection_executor) calls throttle.emit(...) directly on
        # this SAME instance for PlaceStop/UpgradeToOco/AmendStop/AlertOnly,
        # bypassing deps.alert_throttled entirely — teeing the base sink is
        # the only seam that catches every throttled send uniformly (see
        # service.InProcessManagerService's class docstring). The throttle
        # itself is built ONCE here (persists dedup/escalation state across
        # ticks); the CURRENT event-capturing closure is rebound on every
        # _deps_factory call (the service hands a fresh one each cycle).
        self._current_event_alert_throttled = lambda message, reason: None
        self._throttle = cl._AlertThrottle(self._throttle_base_sink)

        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.journal = root / "standalone_stops.jsonl"
        self.kill_file = root / "KILL"
        # A GLOBAL kill path that is never written to by default (D3, ADR
        # 0016) — kept distinct from self.kill_file (the per-instance kill)
        # and pinned to this test's own temp dir so the world never probes
        # the real ~/.alphalens/broker_orders/KILL default.
        self.global_kill_file = root / "GLOBAL_KILL"

        self._chain = _Chain(alive=True)
        self.picks_placed: list[Any] = []
        self._verdicts: list[Any] = []
        self._working_children: dict[str, tuple[str, ...]] = {}
        self._oco_enabled = False
        self._amend_enabled = False
        # INC-5 live-exits state: prices the world's _WorldPriceFeed serves,
        # keyed by uic. Empty by default -> every uic reads stale (None), so a
        # scenario that never calls price_is never fires a tranche even with
        # the flag on.
        self._live_exit_prices: dict[int, float] = {}

        # Orders are ON by default (the disaster-stop rail must always be able to
        # place); the exit mechanisms default OFF exactly like production.
        self._env = mock.patch.dict(os.environ, {"ALPHALENS_BROKER_ALLOW_ORDERS": "1"}, clear=False)
        self._env.start()
        self._journal_patch = mock.patch.object(
            cl, "_standalone_stop_journal_path", lambda: self.journal
        )
        self._journal_patch.start()

        # PR-8: the world drives the manager THROUGH the ManagerService
        # Protocol boundary — the in-process transport wrapping the SAME
        # deps this world has always built.
        self._service = service.InProcessManagerService(self._deps_factory)

        test.addCleanup(self._close)

    def _close(self) -> None:
        self._journal_patch.stop()
        self._env.stop()
        self._tmp.cleanup()

    # ==== GIVEN ================================================================

    def arm(self, ticker: str) -> None:
        """A human arms this ticker (records the intent in the pick queue)."""
        self._service.submit_intent(_pick(ticker))

    def entry_fills(
        self,
        ticker: str,
        *,
        shares: float,
        price: float = _DEFAULT_FILL,
        stop: float = _DEFAULT_STOP,
        take_profit: float | None = _DEFAULT_TP,
    ) -> None:
        """An entry fills: the position appears AND its plan (the disaster stop +
        take-profit prices) is on record — the normal "a trade opened" setup."""
        self.broker.set_position(ticker, shares, avg_price=price)
        self._seed_plan(ticker, stop=stop, take_profit=take_profit)

    def entry_fills_with_tranches(
        self,
        ticker: str,
        *,
        shares: float,
        price: float = _DEFAULT_FILL,
        stop: float = _DEFAULT_STOP,
        tranches: tuple[Any, ...],
    ) -> None:
        """An entry fills AND carries a live-exit TP ladder on record (INC-5) —
        the setup a position placed AFTER the tranche_plan journaling deploy
        has. ``tranches`` is a tuple of ``broker_contract.sizing.TpTranchePlan``.
        Distinct from ``entry_fills`` (which seeds only the legacy scalar
        ``planned`` line) so pre-INC-5 scenarios are unaffected."""
        self.broker.set_position(ticker, shares, avg_price=price)
        self._seed_plan(ticker, stop=stop, take_profit=None)
        uic = self.broker.uic_of(ticker)
        cl._append_standalone_stop_journal(
            cl._build_tranche_plan_line(
                uic=uic, tp_tranches=tranches, reference_qty=shares, stop_price=stop
            )
        )

    def live_exits_are_enabled(self) -> None:
        """The live TP-tranche exit engine tick phase is armed (INC-5)."""
        os.environ["ALPHALENS_LIVE_MARKET_EXITS"] = "1"

    def price_is(self, ticker: str, price: float) -> None:
        """The live price feed reads this price for ``ticker`` this tick."""
        self._live_exit_prices[self.broker.uic_of(ticker)] = price

    def market_sell_fails_once(self, error: Exception) -> None:
        """The NEXT market sell (only) fails at the broker — used to prove the
        SL, already shrunk by the preceding amend, is re-grown by the very next
        protection pass in the SAME tick (the failed-sell re-cover proof)."""
        self.broker.market_order_error = error

    def already_holds(self, ticker: str, *, shares: float, price: float = _DEFAULT_FILL) -> None:
        """The account already holds a position with NO plan on record — an
        orphan the manager discovers from live broker state alone."""
        self.broker.set_position(ticker, shares, avg_price=price)

    def grows_position_to(
        self, ticker: str, *, shares: float, price: float = _DEFAULT_FILL
    ) -> None:
        """The position grows (a later tranche fills) to a new share count."""
        self.broker.set_position(ticker, shares, avg_price=price)

    def has_resting_stop(self, ticker: str, *, shares: float, price: float = _DEFAULT_STOP) -> str:
        """A protective stop already rests on the position."""
        return self.broker.add_resting_sell(ticker, shares, price, order_type="StopIfTraded")

    def broker_rejects_oco(self, *, code: str = "TooFarFromMarket") -> None:
        """The broker will refuse any OCO placement with this reject code."""
        self.broker.oco_reject_code = code

    def broker_placement_fails_on(self, ticker: str) -> None:
        """Every write to this ticker's instrument fails at the broker."""
        self.broker.failing_uics.add(self.broker.uic_of(ticker))

    def cancel_fails_for(self, order_id: str, error: Exception) -> None:
        self.broker.cancel_errors[order_id] = error

    def orders_are_disabled(self) -> None:
        """The master order switch is off (ALPHALENS_BROKER_ALLOW_ORDERS != 1)."""
        os.environ["ALPHALENS_BROKER_ALLOW_ORDERS"] = "0"

    def kill_switch_is_pulled(self) -> None:
        """The emergency KILL file is present."""
        self.kill_file.write_text("stop")

    def auth_chain_is_dead(self) -> None:
        """The broker session/auth chain is down."""
        self._chain = _Chain(alive=False, reason="session token expired")

    def oco_is_enabled(self) -> None:
        os.environ["ALPHALENS_BROKER_OCO_ENABLED"] = "1"
        self._oco_enabled = True

    def amend_is_enabled(self) -> None:
        os.environ["ALPHALENS_BROKER_AMEND_ENABLED"] = "1"
        self._amend_enabled = True

    def broker_reports(self, *verdicts: Any) -> None:
        """The reconcile step will report these order outcomes this tick."""
        self._verdicts = list(verdicts)

    def working_children_for(self, request_id: str, order_ids: tuple[str, ...]) -> None:
        self._working_children[request_id] = order_ids

    # ==== WHEN =================================================================

    def run_tick(self) -> cl.TickReport:
        """The manager runs one management tick THROUGH the ManagerService
        Protocol (PR-8), then drains the cycle's events into
        ``_captured_alert_events`` so the THEN-side asserts have something to
        read (the drain happens here, once per tick — assert_alerted /
        assert_silent never drain themselves, so multiple asserts in one test
        still see the same captured alerts)."""
        report = self._service.run_cycle()
        for event in self._service.stream_events():
            if isinstance(event, service.AlertEvent):
                self._captured_alert_events.append(event.message)
        return report

    def run_ticks(self, count: int) -> cl.TickReport:
        report = cl.TickReport()
        for _ in range(count):
            report = self.run_tick()
        return report

    # ==== THEN =================================================================

    def assert_protected(self, ticker: str) -> None:
        """The position is covered by a resting protective stop for all its shares."""
        owned = self._owned(ticker)
        stop_qty = self._resting_stop_qty(ticker)
        if stop_qty + _QTY_EPS < owned:
            raise AssertionError(
                f"{ticker} is NAKED: owns {owned} but only {stop_qty} covered by a stop"
            )

    def assert_protected_by_oco(self, ticker: str) -> None:
        legs = self._sell_legs(ticker)
        oco = [o for o in legs if o.order_relation == "Oco"]
        if len(oco) < 2:
            raise AssertionError(f"{ticker} has no resting OCO pair (found legs: {legs})")
        self.assert_protected(ticker)

    def assert_flat(self, ticker: str) -> None:
        owned = self._owned(ticker)
        if owned > _QTY_EPS:
            raise AssertionError(f"{ticker} is not flat: owns {owned}")

    def assert_not_oversold(self, ticker: str) -> None:
        """The manager never commits to sell more shares than it owns (an OCO
        pair counts once, not twice)."""
        owned = self._owned(ticker)
        commitment = self._sell_commitment(ticker)
        if commitment > owned + _QTY_EPS:
            raise AssertionError(
                f"{ticker} is OVERSOLD: owns {owned} but resting SELL commitment is {commitment}"
            )

    def assert_no_new_orders(self, before: int) -> None:
        after = len(self.broker.list_working_sell_orders())
        if after != before:
            raise AssertionError(f"expected no new orders, went from {before} to {after}")

    def assert_exactly_covered(self, ticker: str) -> None:
        """The resting protective stop qty is EXACTLY the owned qty — neither
        naked (under) nor over-hedged (a stale, un-shrunk stop) — the shape a
        correctly shrunk SL must have after a live-exit tranche fires."""
        owned = self._owned(ticker)
        stop_qty = self._resting_stop_qty(ticker)
        if abs(stop_qty - owned) > _QTY_EPS:
            raise AssertionError(
                f"{ticker}: stop qty {stop_qty} != owned {owned} (expected an exact match)"
            )

    def owned(self, ticker: str) -> float:
        return self._owned(ticker)

    def resting_stop_qty(self, ticker: str) -> float:
        return self._resting_stop_qty(ticker)

    def resting_order_count(self) -> int:
        return len(self.broker.list_working_sell_orders())

    def assert_picks_placed(self, count: int) -> None:
        if len(self.picks_placed) != count:
            raise AssertionError(f"expected {count} picks placed, got {len(self.picks_placed)}")

    def assert_alerted(self, *, containing: str) -> None:
        if not any(containing.lower() in a.lower() for a in self._captured_alert_events):
            raise AssertionError(
                f"expected an alert containing {containing!r}; "
                f"alert events were: {self._captured_alert_events}"
            )

    def assert_silent(self) -> None:
        if self._captured_alert_events:
            raise AssertionError(f"expected no alerts, got: {self._captured_alert_events}")

    def assert_order_gone(self, order_id: str) -> None:
        if self.broker.has_order(order_id):
            raise AssertionError(
                f"expected order {order_id} to be cancelled, but it is still resting"
            )

    def safety_allows_a_new_pick(
        self,
        *,
        open_brackets: int = 0,
        open_positions: int = 0,
        realized_r_today: float = 0.0,
        equity: float = 1_000_000.0,
    ) -> bool:
        """Run the REAL safety gate under the current world (env + KILL file)."""
        decision = safety.check(
            _pick("KO"),
            safety.JournalView(
                open_bracket_count=open_brackets,
                realized_r_today=realized_r_today,
            ),
            safety.BrokerView(open_position_count=open_positions, equity=equity),
            self._chain,
            kill_path=self.kill_file,
            global_kill_path=self.global_kill_file,
        )
        return isinstance(decision, safety.Allow)

    def safety_refusal_reason(self, **kw: Any) -> str:
        decision = safety.check(
            _pick("KO"),
            safety.JournalView(
                open_bracket_count=kw.get("open_brackets", 0),
                realized_r_today=kw.get("realized_r_today", 0.0),
            ),
            safety.BrokerView(
                open_position_count=kw.get("open_positions", 0),
                equity=kw.get("equity", 1_000_000.0),
            ),
            self._chain,
            kill_path=self.kill_file,
            global_kill_path=self.global_kill_file,
        )
        return getattr(decision, "reason", "")

    # ==== internals ============================================================

    def _throttle_base_sink(self, message: str) -> None:
        """The shared _AlertThrottle's underlying sink (built once in
        __init__). Feeds the real delivery list (self.alerts, unchanged) AND
        tees to whichever event-capturing closure the service handed the
        MOST RECENT ``_deps_factory`` call — see the __init__ comment."""
        self.alerts.append(message)
        self._current_event_alert_throttled(message, "")

    def _deps_factory(
        self,
        event_alert: Any,
        event_alert_throttled: Any,
        picks: list[Any],
    ) -> cl.LoopDeps:
        """Build ONE cycle's real, fully-wired LoopDeps for the
        InProcessManagerService (PR-8) — the same wiring this world has
        always built, now handed the service's event-capturing alert sinks
        + its internal pick queue instead of the world's own ``self._picks``.
        """
        self._current_event_alert_throttled = event_alert_throttled

        def alert(message: str) -> None:
            self.alerts.append(message)
            event_alert(message)

        def alert_throttled(message: str, reason: str) -> bool:
            return self._throttle.emit(message, reason=reason)

        oco_placer = self.broker.place_oco_exit if self._oco_enabled else None
        amend_placer = self.broker.amend_stop_amount if self._amend_enabled else None
        executor = cl._make_protection_executor(
            self.broker, self._throttle, place_oco_exit=oco_placer, amend_stop=amend_placer
        )
        return cl.LoopDeps(
            broker=self.broker,
            kill_file=self.kill_file,
            global_kill_file=self.global_kill_file,
            ensure_alive=lambda: self._chain,
            iter_picks=lambda: iter(picks),
            place_pick=self._place_pick,
            # No submission records: the protection pass reads live broker state,
            # not these records (build_protection_view ignores its records arg).
            read_records=list,
            verdicts_fn=lambda _records, _broker: list(self._verdicts),
            build_position_view=lambda _broker, _records: cl.BrokerView(
                working_children=dict(self._working_children)
            ),
            build_protection_view=cl.build_protection_view,
            execute_protection=executor,
            sweep_orphans_fn=lambda _broker: [],
            alert=alert,
            alert_throttled=alert_throttled,
            place_oco_exit=oco_placer,
            amend_stop=amend_placer,
            # INC-5: the world's own fake feed, reading whatever price_is() set.
            # A uic price_is never touched reads stale (None) -> stream-health
            # veto, so scenarios that never call price_is never fire a tranche
            # even with live_exits_are_enabled() on.
            live_exits_feed_factory=lambda _uic_to_ticker, *, scope: _WorldPriceFeed(
                self._live_exit_prices
            ),
        )

    def _place_pick(self, pick: Any) -> bool:
        self.picks_placed.append(pick)
        return True

    def _seed_plan(self, ticker: str, *, stop: float, take_profit: float | None) -> None:
        cl._append_standalone_stop_journal(
            cl._build_planned_line(
                entry_crid=f"crid-{ticker.upper()}",
                uic=self.broker.uic_of(ticker),
                side="SELL",
                stop_price=stop,
                take_profit=take_profit,
                tier_index=0,
            )
        )

    def _owned(self, ticker: str) -> float:
        pos = self.broker.get_positions_by_uic(self.broker.uic_of(ticker))
        return max(0.0, pos.quantity)

    def _sell_legs(self, ticker: str) -> list[Any]:
        uic = self.broker.uic_of(ticker)
        return [o for o in self.broker.list_working_sell_orders() if o.uic == uic]

    def _resting_stop_qty(self, ticker: str) -> float:
        return sum(
            (o.amount or 0.0)
            for o in self._sell_legs(ticker)
            if o.order_type in ("StopIfTraded", "Stop", "TrailingStopIfTraded")
        )

    def _sell_commitment(self, ticker: str) -> float:
        legs = self._sell_legs(ticker)
        oco_amounts = [o.amount or 0.0 for o in legs if o.order_relation == "Oco"]
        plain = sum((o.amount or 0.0) for o in legs if o.order_relation != "Oco")
        # An OCO pair is one commitment (either the stop OR the tp fills, not both).
        oco = max(oco_amounts) if oco_amounts else 0.0
        return plain + oco


def _pick(ticker: str) -> Any:
    """A minimal, structurally valid armed TradeIntent (PR-7: the real
    _run_placement_drain calls the real _pick_key, which reads
    intent.instrument.ticker / intent.meta.brief_date)."""
    from broker_contract.trade_intent.schema import (
        EntryTierSpec,
        InstrumentHint,
        IntentMeta,
        TradeIntent,
        TradeSpec,
    )

    return TradeIntent(
        intent_id=f"{ticker.upper()}:2026-07-23",
        instrument=InstrumentHint(ticker=ticker.upper(), mic="XNYS"),
        spec=TradeSpec(
            entry_tiers=(EntryTierSpec(limit_price=100.0, alloc_pct=100.0),),
            disaster_stop=90.0,
            tp_tranches=(),
            suggested_size_pct=3.0,
        ),
        meta=IntentMeta(armed_ts="2026-07-23T00:00:00+00:00", brief_date="2026-07-23"),
    )


# Re-exported so scenario modules can build broker outcome rows readably.
def order_filled(ticker: str, *, shares: float, closed_r: float | None = None) -> Any:
    from alphalens_pipeline.brokers.reconcile import ReconcileVerdict

    verdict = "FILLED" if closed_r is None else f"FILLED(closed r={closed_r:+.2f})"
    return ReconcileVerdict(
        brief_date="2026-07-23",
        ticker=ticker.upper(),
        qty=float(shares),
        entry_order_id=f"entry-{ticker.upper()}",
        status=OrderStatus.FILLED.value,
        verdict=verdict,
    )


def order_cancelled(ticker: str, *, request_id: str = "entry-KO") -> Any:
    from alphalens_pipeline.brokers.reconcile import ReconcileVerdict

    return ReconcileVerdict(
        brief_date="2026-07-23",
        ticker=ticker.upper(),
        qty=0.0,
        entry_order_id=request_id,
        status=OrderStatus.CANCELLED.value,
        verdict="CANCELLED",
    )


def a_divergence(ticker: str) -> Any:
    """A reconcile row where the journal and the broker disagree — must be
    surfaced to the operator, never silently accepted."""
    from alphalens_pipeline.brokers.reconcile import ReconcileVerdict

    return ReconcileVerdict(
        brief_date="2026-07-23",
        ticker=ticker.upper(),
        qty=100.0,
        entry_order_id=f"entry-{ticker.upper()}",
        status=OrderStatus.WORKING.value,
        verdict="WORKING(diverged)",
        divergence=True,
    )

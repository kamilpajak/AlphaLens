"""ManagerWorld — the scenario DSL for the acceptance suite.

Every acceptance test reads as a sentence: a few GIVEN calls set the scene, one
WHEN call runs a management tick, and one THEN call checks a guarantee. All the
mechanics the auto-manager actually uses — uic numbers, the plan journal, the
env flags, the alert plumbing — live in here so the tests stay in plain business
language.

The tick runs the REAL manager (`control_loop.run_once` with the real
`build_protection_view`, the real reconcile, and the real protection executor)
against the fake in-memory broker. Nothing about the tick is stubbed except the
inputs a human would set up (positions, plans, faults).
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager import safety
from alphalens_pipeline.brokers.contract import OrderStatus

from .fake_broker import FakeBroker

_QTY_EPS = 0.5
_DEFAULT_STOP = 44.0
_DEFAULT_TP = 59.0
_DEFAULT_FILL = 50.0


class _Chain:
    """A tiny session-chain status object (what ensure_alive returns)."""

    def __init__(self, *, alive: bool, reason: str = "") -> None:
        self.alive = alive
        self.reason = reason


class ManagerWorld:
    """A single self-contained world the manager runs inside.

    Construct one per test: ``world = ManagerWorld(self)``. It registers its own
    cleanup on the test, so tests never manage tempfiles or env vars by hand.
    """

    def __init__(self, test: unittest.TestCase, *, equity: float = 1_000_000.0) -> None:
        self.broker = FakeBroker(equity=equity)
        self.alerts: list[str] = []
        self._throttle = cl._AlertThrottle(self.alerts.append)

        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.journal = root / "standalone_stops.jsonl"
        self.kill_file = root / "KILL"

        self._chain = _Chain(alive=True)
        self._picks: list[Any] = []
        self.picks_placed: list[Any] = []
        self._verdicts: list[Any] = []
        self._working_children: dict[str, tuple[str, ...]] = {}
        self._oco_enabled = False
        self._amend_enabled = False

        # Orders are ON by default (the disaster-stop rail must always be able to
        # place); the exit mechanisms default OFF exactly like production.
        self._env = mock.patch.dict(os.environ, {"ALPHALENS_BROKER_ALLOW_ORDERS": "1"}, clear=False)
        self._env.start()
        self._journal_patch = mock.patch.object(cl, "STANDALONE_STOP_JOURNAL_PATH", self.journal)
        self._journal_patch.start()

        test.addCleanup(self._close)

    def _close(self) -> None:
        self._journal_patch.stop()
        self._env.stop()
        self._tmp.cleanup()

    # ==== GIVEN ================================================================

    def arm(self, ticker: str) -> None:
        """A human arms this ticker (records the intent in the pick queue)."""
        self._picks.append(_pick(ticker))

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
        """The manager runs one management tick."""
        return cl.run_once(self._deps())

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

    def resting_order_count(self) -> int:
        return len(self.broker.list_working_sell_orders())

    def assert_picks_placed(self, count: int) -> None:
        if len(self.picks_placed) != count:
            raise AssertionError(f"expected {count} picks placed, got {len(self.picks_placed)}")

    def assert_alerted(self, *, containing: str) -> None:
        if not any(containing.lower() in a.lower() for a in self.alerts):
            raise AssertionError(
                f"expected an alert containing {containing!r}; alerts were: {self.alerts}"
            )

    def assert_silent(self) -> None:
        if self.alerts:
            raise AssertionError(f"expected no alerts, got: {self.alerts}")

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
        gross_committed: float = 0.0,
        realized_r_today: float = 0.0,
        equity: float = 1_000_000.0,
    ) -> bool:
        """Run the REAL safety gate under the current world (env + KILL file)."""
        decision = safety.check(
            _pick("KO"),
            safety.JournalView(
                open_bracket_count=open_brackets,
                gross_committed=gross_committed,
                realized_r_today=realized_r_today,
            ),
            safety.BrokerView(open_position_count=open_positions, equity=equity),
            self._chain,
            kill_path=self.kill_file,
        )
        return isinstance(decision, safety.Allow)

    def safety_refusal_reason(self, **kw: Any) -> str:
        decision = safety.check(
            _pick("KO"),
            safety.JournalView(
                open_bracket_count=kw.get("open_brackets", 0),
                gross_committed=kw.get("gross_committed", 0.0),
                realized_r_today=kw.get("realized_r_today", 0.0),
            ),
            safety.BrokerView(
                open_position_count=kw.get("open_positions", 0),
                equity=kw.get("equity", 1_000_000.0),
            ),
            self._chain,
            kill_path=self.kill_file,
        )
        return getattr(decision, "reason", "")

    # ==== internals ============================================================

    def _deps(self) -> cl.LoopDeps:
        oco_placer = self.broker.place_oco_exit if self._oco_enabled else None
        amend_placer = self.broker.amend_stop_amount if self._amend_enabled else None
        executor = cl._make_protection_executor(
            self.broker, self._throttle, place_oco_exit=oco_placer, amend_stop=amend_placer
        )
        return cl.LoopDeps(
            broker=self.broker,
            kill_file=self.kill_file,
            ensure_alive=lambda: self._chain,
            iter_picks=lambda: iter(self._picks),
            place_pick=self._place_pick,
            read_records=list,
            verdicts_fn=lambda _records, _broker: list(self._verdicts),
            build_position_view=lambda _broker, _records: cl.BrokerView(
                working_children=dict(self._working_children)
            ),
            build_protection_view=cl.build_protection_view,
            execute_protection=executor,
            sweep_orphans_fn=lambda _broker: [],
            alert=self.alerts.append,
            alert_throttled=lambda message, reason: self._throttle.emit(message, reason=reason),
            place_oco_exit=oco_placer,
            amend_stop=amend_placer,
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
    """A minimal armed pick (only the fields the gate + drain read)."""
    import datetime as dt

    from alphalens_pipeline.brokers.automanager.picks import Pick

    return Pick(
        ticker=ticker.upper(),
        date=dt.date(2026, 7, 23),
        armed_ts="2026-07-23T00:00:00+00:00",
        status="armed",
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

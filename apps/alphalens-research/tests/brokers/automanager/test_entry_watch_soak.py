"""Multi-session SOAK of the entry-trailing lifecycle through ``run_once`` (#9).

One end-to-end journey of a laddered pick across >=5 logical session boundaries,
driven ONLY through ``cl.run_once(deps)`` (never a phase helper directly) so it
proves the whole-tick phase ordering — fill-reconcile BEFORE the placement drain
BEFORE the watch pass — holds tick after tick, session after session. A single
continuous ``entry_trails.jsonl`` journal + one broker + one deps carry the
state across every tick; only the injected price feed, the broker's
resolutions/open-orders, and the drained pick queue change between ticks (exactly
what changes between real 45s daemon ticks).

The journey chains, in order, every terminal + re-admit transition the feature
owns:

- (a) ARM   — a drained pick opens per-tier ``watch_open`` reservations;
- (b) TOUCH — a **latch-only sub-tick wick** (the coarse point-sample stays
              ABOVE the tier limit; the 1 Hz ``session_low`` dips BELOW it) is
              the ONLY reason tier-0 touches + arms its native trailing order —
              so this soak exercises the touch-latch combine (#5) THROUGH
              ``run_once``, not a bypass;
- (c) FILL  — the reconcile pass writes the terminal ``fired`` line and the
              virtual reservation drops to the remaining live tiers;
- (d) RE-ARM — an overnight DayOrder-cancel re-admits a tier with its trough
              CARRIED and the open-check armed, TTL ``window_end`` UNCHANGED;
- (e) EXPIRY — a later tier crosses its ORIGINAL ``window_end`` and terminates;
- (f) SUSPEND — a G9 deep decline suspends a tier AND cancels its resting order;
- (g) GROSS-CAP — a fresh pick is REFUSED (``picks.mark_refused``) purely because
              the accumulated watching reservations from prior transitions push
              it over the cap.

The ONE non-negotiable invariant, asserted after EVERY tick:
``watching_virtual_gross_acct(read_entry_trail_fold())`` equals the independent
sum of ``limit x qty`` over the LIVE (non-terminal) tiers, with zero unvaluable
records — no double count, terminals drop to exactly 0. If the touch-latch,
reconcile, re-arm or suspend logic mis-accounted a reservation, this fails.

Hermetic: no network, no sleeps, deterministic. The single ``cl.dt.datetime.now``
mock (the TTL-expiry tick) is tightly scoped and restored.
"""

from __future__ import annotations

import datetime as dt
import unittest
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager import entry_trails
from broker_contract.contract import OrderStatus

# Shared hermetic fixtures (task-mandated reuse — do not reinvent).
from tests.brokers.automanager.test_entry_watch_acceptance import _empty_pview
from tests.brokers.automanager.test_entry_watch_reconcile import _os, _ResolvingBroker
from tests.brokers.automanager.test_entry_watch_wiring import (
    _FakeFeed,
    _journal,
    _lines,
    _placement,
    _plan_with_tranches,
    _planned_journal,
    _tranche,
)

_ENV = entry_trails.ENTRY_TRAIL_BPS_ENV


def _plan_l(*tiers: tuple[int, float, int]):
    """A SetupPlan with the brief's thirds TP ladder attached, targets far above
    every price this file ticks — the router journals a ``tranche_plan`` only
    for a non-empty ladder, and the #1112 brief-ladder arm gate fails CLOSED on
    a missing plan, so a trancheless SetupPlan routes a watch production could
    never produce."""
    return _plan_with_tranches(
        tuple(tiers),
        (_tranche(0, 1000.0, 1 / 3), _tranche(1, 1005.0, 1 / 3), _tranche(2, 1010.0, 1 / 3)),
    )


_ALLOW_ORDERS_ENV = "ALPHALENS_BROKER_ALLOW_ORDERS"
_GROSS_FRAC_ENV = "ALPHALENS_BROKER_PORTFOLIO_GROSS_FRAC"

_UIC_KO = 307
_UIC_NEWCO = 401

# GROSS_FRAC 0.4 x total_value 100_000 = a 40_000 account-currency gross cap.
# The initial 3-tier arm (37_500) clears it; the competitor drain (g) crosses it
# only BECAUSE the prior transitions left 17_500 still reserved.
_SOAK_ENV = {_ENV: "50", _ALLOW_ORDERS_ENV: "1", _GROSS_FRAC_ENV: "0.4"}


def _mk_instr(uic: int) -> Any:
    return type("I", (), {"currency": "USD", "broker_instrument_id": uic, "exchange_mic": "XNYS"})()


def _mk_pick(ticker: str, date: str, plan: Any, mic: str = "XNYS") -> Any:
    """An armed intent carrying its sized plan on the spec (the ``compute_setup_plan``
    stub reads ``spec.soak_plan``, so each pick sizes to its OWN plan)."""
    spec = type("Spec", (), {"entry_tiers": ("t",), "soak_plan": plan})()
    return type(
        "Intent",
        (),
        {
            "instrument": type("Hint", (), {"ticker": ticker, "mic": mic})(),
            "meta": type("Meta", (), {"brief_date": date, "source": "brief"})(),
            "spec": spec,
            "exit": None,
        },
    )()


def _straggler(external_reference: str, order_id: str) -> Any:
    """An open-orders entry (duck-typed like the wiring/reconcile fixtures) — a
    resting ``-entry-`` order the finalize-vs-broker cancel path can find."""
    return type(
        "OS",
        (),
        {"order_id": order_id, "external_reference": external_reference, "side": "BUY"},
    )()


class _FrozenNow(dt.datetime):
    """A ``datetime`` whose ``now`` returns a fixed instant — used ONLY to push
    one tick past the TTL ``window_end`` (the pure engine's expiry is time-based).
    Every other classmethod (``fromisoformat`` etc.) is inherited unchanged."""

    _instant = dt.datetime(2027, 1, 1, tzinfo=dt.UTC)

    @classmethod
    def now(cls, tz: Any = None) -> dt.datetime:
        # tz is part of datetime.now's signature; ignored — the instant is fixed.
        return cls._instant


class TestEntryWatchMultiSessionSoak(unittest.TestCase):
    def _build_deps(self, broker: Any, picks: list[Any]) -> cl.LoopDeps:
        instruments = {"KO": _mk_instr(_UIC_KO), "NEWCO": _mk_instr(_UIC_NEWCO)}
        pkg = "alphalens_pipeline.brokers"
        for target, fn in (
            (f"{pkg}.automanager.reconcile_bridge.verdicts", lambda _r, _b, **_k: []),
            (f"{pkg}.automanager.safety.check", lambda *_a, **_k: object()),
            (f"{pkg}.routing.resolve_us_instrument", lambda _b, t, **_kw: instruments[t]),
            (f"{pkg}.submission_log.iter_submission_records", lambda _p: []),
            (f"{pkg}.submission_log.build_submission_record", lambda **kw: dict(kw)),
            (f"{pkg}.submission_log.append_submission_record", lambda _r: None),
            ("broker_contract.sizing.compute_setup_plan", lambda s, **_k: s.soak_plan),
            (f"{pkg}.automanager.placement_planner.classify", lambda *_a, **_k: _placement()),
            (f"{pkg}.automanager.picks.mark_refused", self._record_refused),
        ):
            self.enterContext(mock.patch(target, fn))
        # Real tranche_plan writes land in a temp journal — the #1112
        # brief-ladder arm gate reads them back at touch time (fail-closed).
        _planned_journal(self)

        def _throttled(message: str, reason: str) -> bool:
            self.alerts.append((message, reason))
            return True

        return cl.LoopDeps(
            broker=broker,
            kill_file=cl.Path("/nonexistent/KILL"),
            ensure_alive=lambda: type("C", (), {"alive": True, "reason": None})(),  # noqa: PLW0108
            iter_picks=lambda: iter(picks),
            place_pick=cl._make_place_pick(broker),
            read_records=list,
            verdicts_fn=lambda _r, _b, **_k: [],
            build_position_view=lambda _b, _r: object(),
            build_protection_view=lambda _b, _r: _empty_pview(),
            execute_protection=lambda _a, _k, _r: None,
            sweep_orphans_fn=lambda _b: [],
            alert=lambda _m: None,
            alert_throttled=_throttled,
            live_exits_feed_factory=lambda _u2i, *, scope: self.feed,
        )

    def _record_refused(self, ticker: str, brief_date: Any, reason: str) -> None:
        self.refused.append((ticker, brief_date, reason))

    # --- invariants ----------------------------------------------------------

    def _live_reservation(self) -> float:
        """Independent recompute of the virtual watching gross: ``limit x qty``
        summed over every NON-terminal tier (fx None throughout). Deliberately a
        SECOND implementation of the fold's own arithmetic so a mis-accounting in
        ``watching_virtual_gross_acct`` cannot hide behind itself."""
        fold = entry_trails.read_entry_trail_fold()
        total = 0.0
        for state in fold.tiers.values():
            if state.terminal_kind is not None or state.watch_open is None:
                continue
            total += float(state.watch_open["limit"]) * float(state.watch_open["qty"])
        return total

    def _assert_conservation(self, expected: float, *, tick: str) -> None:
        fold = entry_trails.read_entry_trail_fold()
        total, bad = entry_trails.watching_virtual_gross_acct(fold)
        self.assertEqual(bad, 0, f"{tick}: an unvaluable/malformed reservation appeared")
        self.assertAlmostEqual(
            total,
            self._live_reservation(),
            places=6,
            msg=f"{tick}: fold gross disagrees with the independent live-tier sum",
        )
        self.assertAlmostEqual(total, expected, places=6, msg=f"{tick}: reservation not conserved")

    def _run_tick(
        self, *, tick: str, expected_reservation: float, freeze_now: bool = False
    ) -> None:
        env_ctx = mock.patch.dict("os.environ", _SOAK_ENV, clear=True)
        with env_ctx:
            if freeze_now:
                with mock.patch.object(cl.dt, "datetime", _FrozenNow):
                    cl.run_once(self.deps)
            else:
                cl.run_once(self.deps)
        self._assert_conservation(expected_reservation, tick=tick)

    def _set_feed(self, *, price: float | None, low: float | None) -> None:
        self.prices[_UIC_KO] = price
        self.lows[_UIC_KO] = low

    def _crid(self, tier_index: int) -> str:
        return f"KO-{self.today}-entry-t{tier_index}"

    def _order_id_for(self, fire_ref_fragment: str) -> str:
        matches = [o for o in self.broker.trailing_orders if fire_ref_fragment in o["request_id"]]
        self.assertEqual(
            len(matches), 1, f"expected exactly one armed order for {fire_ref_fragment}"
        )
        return matches[0]["order_id"]

    # --- the soak ------------------------------------------------------------

    def test_multi_session_lifecycle_conserves_reservation_every_tick(self) -> None:
        self.path = _journal(self)
        self.today = dt.date.today().isoformat()  # recent -> window_end in the future
        self.prices: dict[int, float | None] = {_UIC_KO: None}
        self.lows: dict[int, float | None] = {_UIC_KO: None}
        self.feed = _FakeFeed(self.prices, self.lows)
        self.alerts: list[tuple[str, str]] = []
        self.refused: list[tuple[str, Any, str]] = []
        self.broker = _ResolvingBroker()

        # A laddered pick (strictly-descending tiers, all uic 307): t0 the shallow
        # latch-fill tier, t1 the deep-decline suspend tier, t2 the re-arm/expiry
        # tier. Reservation at open = 20_000 + 12_500 + 5_000 = 37_500.
        ko_plan = _plan_l((0, 40.0, 500), (1, 25.0, 500), (2, 10.0, 500))
        newco_plan = _plan_l((0, 250.0, 100))  # gross 25_000 — the (g) competitor
        self.picks: list[Any] = []
        self.deps = self._build_deps(self.broker, self.picks)

        # ============================================================= SESSION 1
        # (a) ARM: drain the laddered pick -> three watch_open reservations. The
        # high point-sample keeps every tier WATCHING and stamps the latch's
        # freshness reference (the first fresh tick a brand-new watcher needs
        # before its 1 Hz latch is trusted on tick 2).
        self.picks[:] = [_mk_pick("KO", self.today, ko_plan)]
        self._set_feed(price=100.0, low=None)
        self._run_tick(tick="S1 arm", expected_reservation=37_500.0)
        self.picks.clear()  # pick retired from the drain

        watch_opens = [ln for ln in _lines(self.path) if ln["kind"] == entry_trails.KIND_WATCH_OPEN]
        self.assertEqual(len(watch_opens), 3, "one watch_open per positive-qty tier")
        # Capture t2's ORIGINAL TTL window_end — the re-arm must NEVER extend it.
        t2_window_end = (
            entry_trails.read_entry_trail_fold().tiers[self._crid(2)].watch_open["window_end"]
        )

        # ============================================================= SESSION 2
        # (b) TOUCH via the touch-latch ONLY: the point-sample bid (40.50) stays
        # ABOVE tier-0's 40.00 limit — a coarse 45s sample that MISSED the dip —
        # but the 1 Hz session_low wicked to 39.50. The combine's min() folds the
        # sub-tick low in, registers the touch, and the native trailing order arms.
        # No point-sample alone could have touched here: that is the whole proof
        # #9 drives #5 through run_once.
        self.assertGreater(40.50, 40.0, "the point-sample never reaches the tier on its own")
        self._set_feed(price=40.50, low=39.50)
        self._run_tick(tick="S2 latch-wick touch", expected_reservation=37_500.0)

        self.assertEqual(len(self.broker.trailing_orders), 1, "the wick armed exactly one tier")
        t0_order_id = self._order_id_for(f"{self._crid(0)}-fire")
        touched = [ln for ln in _lines(self.path) if ln["kind"] == entry_trails.KIND_TOUCHED]
        self.assertEqual([ln["crid"] for ln in touched], [self._crid(0)], "only tier-0 touched")

        # ------------------------------------------------------------ SESSION 2b
        # (c) FILL: the resting native order fills; the reconcile pass (which runs
        # FIRST in run_once, before the drain) writes the terminal `fired` line and
        # releases tier-0's 20_000 reservation. A high point keeps t1/t2 untouched.
        self.broker.resolutions[t0_order_id] = _os(
            t0_order_id, OrderStatus.FILLED, filled_quantity=500.0, avg_fill_price=39.60
        )
        self._set_feed(price=100.0, low=None)
        self._run_tick(tick="S2b reconcile fill", expected_reservation=17_500.0)

        fired = [ln for ln in _lines(self.path) if ln["kind"] == entry_trails.KIND_FIRED]
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["crid"], self._crid(0))
        self.assertEqual(fired[0]["realized_qty"], 500.0)

        # ============================================================= SESSION 3
        # (g) GROSS-CAP refusal: a NEW pick drains while t1 + t2 (17_500) still
        # reserve. 25_000 candidate + 17_500 watching = 42_500 > 40_000 cap ->
        # terminal refusal via picks.mark_refused. This is a consequence of the
        # ACCUMULATED reservations left by the prior arm + fill, exactly memo G5.
        self.picks[:] = [_mk_pick("NEWCO", self.today, newco_plan)]
        self._set_feed(price=100.0, low=None)
        self._run_tick(tick="S3 gross-cap refusal", expected_reservation=17_500.0)
        self.picks.clear()

        self.assertEqual([r[0] for r in self.refused], ["NEWCO"], "the competitor was refused")
        self.assertIn("gross cap", self.refused[0][2])
        newco_opens = [
            ln
            for ln in _lines(self.path)
            if ln["kind"] == entry_trails.KIND_WATCH_OPEN and ln.get("ticker") == "NEWCO"
        ]
        self.assertEqual(newco_opens, [], "a refused pick opens no watch")
        self.assertEqual(len(self.broker.trailing_orders), 1, "the refusal placed nothing")

        # ============================================================= SESSION 4
        # (f) DEEP-DECLINE SUSPEND with a resting-order cancel. Seed tier-1 into
        # arm-in-progress (a G3 null-id write-ahead whose POST rested at the broker
        # but whose id-journal was lost) + a resting straggler on the book, and
        # evict its runtime so it reconstructs from the fold as TOUCHED. A gap-down
        # to 9.00 drives tier-1's trough (9.00) below its next-tier limit (10.00)
        # -> SUSPENDED, and the finalize-vs-broker cancel takes the resting order
        # off the book. The DEEPEST tier-2 (limit 10) legitimately touches + arms
        # on the same decline (that fall is its job) — which sets up (d)/(e).
        t1_crid = self._crid(1)
        entry_trails.append_entry_trail_line(
            {
                "kind": entry_trails.KIND_TRAIL_ARMED,
                "crid": t1_crid,
                "order_id": None,
                "trigger": 25.5,
            }
        )
        self.broker.open_orders = [_straggler(f"{t1_crid}-fire", "STRAG-T1")]
        self.deps.entry_watchers.pop(t1_crid, None)  # force reconstruction as arm-in-progress
        self._set_feed(price=9.00, low=None)
        self._run_tick(tick="S4 deep-decline suspend", expected_reservation=5_000.0)

        suspended = [ln for ln in _lines(self.path) if ln["kind"] == entry_trails.KIND_SUSPENDED]
        self.assertEqual([ln["crid"] for ln in suspended], [t1_crid], "only tier-1 suspended")
        self.assertIn("STRAG-T1", self.broker.cancels, "the resting -entry- order was cancelled")
        self.assertEqual(
            len(self.broker.trailing_orders), 2, "the deepest tier armed on the decline"
        )
        t2_order_id = self._order_id_for(f"{self._crid(2)}-fire")

        # ============================================================= SESSION 5
        # (d) OVERNIGHT RE-ARM: tier-2's native DayOrder cancelled at the close
        # (resolve -> EXPIRED) BEFORE its TTL window_end. The reconcile pass
        # re-admits it: trough carried, open-check armed, reservation preserved,
        # window_end UNCHANGED. Clear the book so nothing else reconciles.
        self.broker.open_orders = []
        self.broker.resolutions[t2_order_id] = _os(t2_order_id, OrderStatus.EXPIRED)
        self._set_feed(price=100.0, low=None)
        self._run_tick(tick="S5 overnight re-arm", expected_reservation=5_000.0)

        t2_state = entry_trails.read_entry_trail_fold().tiers[self._crid(2)]
        self.assertIsNone(t2_state.terminal_kind, "a re-armed tier is never terminated")
        self.assertEqual(t2_state.latest_kind, entry_trails.KIND_WATCH_OPEN, "back to watching")
        self.assertIsNone(t2_state.armed_order_id, "the stale resting-order id is cleared")
        self.assertEqual(t2_state.min_trough, 9.0, "the trough is carried across the re-arm")
        self.assertTrue(t2_state.watch_open.get("awaiting_fresh_low"), "the open-check is armed")
        self.assertEqual(
            t2_state.watch_open["window_end"], t2_window_end, "the TTL is NEVER extended on re-arm"
        )

        # ============================================================= SESSION 6
        # (e) TTL EXPIRY on the re-armed later tier: one tick whose clock is past
        # the ORIGINAL window_end. The engine's time-based expiry fires even with
        # no fresh price, terminates tier-2 and releases the last reservation to 0.
        self._set_feed(price=None, low=None)
        self._run_tick(tick="S6 ttl expiry", expected_reservation=0.0, freeze_now=True)

        expired = [ln for ln in _lines(self.path) if ln["kind"] == entry_trails.KIND_EXPIRED]
        self.assertEqual([ln["crid"] for ln in expired], [self._crid(2)], "the later tier expired")

        # Final state: every tier terminal, nothing reserved, nothing unvaluable.
        final_fold = entry_trails.read_entry_trail_fold()
        self.assertEqual(
            {c: s.terminal_kind for c, s in final_fold.tiers.items()},
            {
                self._crid(0): entry_trails.KIND_FIRED,
                self._crid(1): entry_trails.KIND_SUSPENDED,
                self._crid(2): entry_trails.KIND_EXPIRED,
            },
        )

    def test_repeated_rearm_never_extends_ttl_or_leaks_reservation(self) -> None:
        """SOAK depth the grand tour proves only ONCE: a tier re-admitted across
        MANY overnight boundaries must carry the SAME original ``window_end`` and
        the SAME reservation every cycle — never a silently-extended TTL nor a
        leaked/double-counted reservation as the re-arm/reconcile loop repeats. A
        bug that only surfaces on the 2nd+ re-admit of one tier is invisible to a
        single-boundary journey; this drives >=3 reconcile re-arm cycles of ONE
        tier through ``cl.run_once`` and asserts both invariants after each."""
        self.path = _journal(self)
        self.today = dt.date.today().isoformat()  # recent -> window_end in the future
        self.prices: dict[int, float | None] = {_UIC_KO: None}
        self.lows: dict[int, float | None] = {_UIC_KO: None}
        self.feed = _FakeFeed(self.prices, self.lows)
        self.alerts: list[tuple[str, str]] = []
        self.refused: list[tuple[str, Any, str]] = []
        self.broker = _ResolvingBroker()

        # One tier — reservation 20_000 — is all this invariant needs.
        ko_plan = _plan_l((0, 40.0, 500))
        self.picks: list[Any] = []
        self.deps = self._build_deps(self.broker, self.picks)

        self.picks[:] = [_mk_pick("KO", self.today, ko_plan)]
        self._set_feed(price=100.0, low=None)
        self._run_tick(tick="arm", expected_reservation=20_000.0)
        self.picks.clear()

        crid = self._crid(0)
        original_window_end = (
            entry_trails.read_entry_trail_fold().tiers[crid].watch_open["window_end"]
        )

        # Re-arm the SAME tier across several overnight boundaries. Each cycle
        # seeds it as armed with a resting DayOrder, expires it (gone from the
        # book + resolves EXPIRED), and lets the reconcile pass re-admit it. The
        # runtime is evicted first so it reconstructs from the fold — the state a
        # real daemon restart/re-arm sees. window_end must stay pinned and the
        # reservation must stay at exactly 20_000, cycle after cycle.
        for cycle in range(3):
            order_id = f"REARM-{cycle}"
            entry_trails.append_entry_trail_line(
                {
                    "kind": entry_trails.KIND_TRAIL_ARMED,
                    "crid": crid,
                    "order_id": order_id,
                    "trigger": 40.2,
                }
            )
            self.broker.open_orders = []
            self.broker.resolutions[order_id] = _os(order_id, OrderStatus.EXPIRED)
            self.deps.entry_watchers.pop(crid, None)  # reconstruct from the fold
            self._set_feed(price=100.0, low=None)
            self._run_tick(tick=f"re-arm cycle {cycle}", expected_reservation=20_000.0)

            state = entry_trails.read_entry_trail_fold().tiers[crid]
            self.assertIsNone(
                state.terminal_kind, f"cycle {cycle}: a re-armed tier is never terminal"
            )
            self.assertEqual(
                state.latest_kind, entry_trails.KIND_WATCH_OPEN, f"cycle {cycle}: back to watching"
            )
            self.assertIsNone(
                state.armed_order_id, f"cycle {cycle}: the stale resting-order id is cleared"
            )
            self.assertTrue(
                state.watch_open.get("awaiting_fresh_low"),
                f"cycle {cycle}: the open-check is re-armed",
            )
            self.assertEqual(
                state.watch_open["window_end"],
                original_window_end,
                f"cycle {cycle}: the TTL was silently extended across repeated re-arm",
            )


if __name__ == "__main__":
    unittest.main()

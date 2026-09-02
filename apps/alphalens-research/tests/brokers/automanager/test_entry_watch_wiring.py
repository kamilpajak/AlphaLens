"""Hermetic tests for the entry-trailing WATCHER WIRING (PR-T1c/d/e).

The engine (``entry_trail_watcher``) is a pure, separately-tested state machine.
These tests pin the INTEGRATION into the daemon:

- the drain intercept inside ``_place_pick`` routes a flag-ON eligible pick into
  a WATCH (per-tier ``watch_open`` lines) and retires it from the drain WITHOUT
  placing a broker order; flag OFF is byte-identical to today;
- the per-tick ``_run_entry_watch_pass`` drives each open watch's state machine
  off an injected price feed, persisting journal intents + terminal measurement
  and routing the throttled alerts, NEVER touching a broker order method;
- KILL gates the pass (memo §3 G2): no journal writes, no alerts under KILL;
- watch capacity is pick-denominated (memo decision #4).

The ONE non-negotiable safety property of PR-T1: no ``place_bracket_order`` /
``place_standalone_stop`` / ``amend_stop_amount`` / ``cancel_order`` is ever
called on the flag-ON path — the "fire" is an alert-only log line.
"""

from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager import entry_trail_watcher, entry_trails
from alphalens_pipeline.brokers.automanager import safety as _safety
from broker_contract.exit_geometry.registry import resolve_exit_policy
from broker_contract.sizing import SetupPlan, TierPlan

from tests.incident_1112_fixture import (
    ETSY_E3_LIMIT,
    ETSY_E3_TARGET,
    SMG_TIERS,
    SMG_TOUCH_BID,
    smg_geometry_stamp,
)

_ENV = entry_trails.ENTRY_TRAIL_BPS_ENV
# Captured at import time (before any test patches the module attribute) so the
# end-to-end MAX_OPEN test can restore the REAL rail over _placer's stub.
_REAL_SAFETY_CHECK = _safety.check


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _pick(ticker: str = "KO", date: str = "2026-07-20", source: str = "brief") -> Any:
    return type(
        "Intent",
        (),
        {
            "instrument": type("Hint", (), {"ticker": ticker, "mic": "XNYS"})(),
            # `source` mirrors IntentMeta's field (#1246: _place_pick threads it
            # into the day-1 gap gate's anchor choice).
            "meta": type("Meta", (), {"brief_date": date, "source": source})(),
            "spec": type("Spec", (), {"entry_tiers": ("t",)})(),
            "exit": None,
        },
    )()


def _plan(*tiers: tuple[int, float, int]) -> SetupPlan:
    """A SetupPlan with the given (tier_index, limit_price, qty) tiers."""
    return SetupPlan(
        suggested_size_pct=1.0,
        scale_factor=1.0,
        final_size_pct=1.0,
        total_notional=sum(limit * qty for _i, limit, qty in tiers),
        paper_equity=100_000.0,
        disaster_stop=8.0,
        order_ttl_days=1,
        entry_tiers=tuple(
            TierPlan(tier_index=i, limit_price=limit, qty=qty, alloc_pct=50.0, tag=f"T{i}")
            for i, limit, qty in tiers
        ),
        tp_tranches=(),
    )


def _instr() -> Any:
    return type("I", (), {"currency": "USD", "broker_instrument_id": 307, "exchange_mic": "XNYS"})()


def _acct() -> Any:
    return type("A", (), {"total_value": 100_000.0, "currency": "USD"})()


def _placed(order_id: str) -> Any:
    return type("Placed", (), {"entry_order_id": order_id, "exit_order_ids": ()})()


class _RecordingBroker:
    """Records every order-facing call. A SupportsTrailingStop (PR-T2b): it can
    place the native trailing-LIMIT entry order the executor arms at TOUCH, so a
    test can assert exactly one trailing order + NO resting-limit bracket."""

    def __init__(self) -> None:
        self.brackets: list[Any] = []
        self.stops: list[Any] = []
        self.amends: list[Any] = []
        self.cancels: list[str] = []
        self.trailing_orders: list[dict[str, Any]] = []
        self.stop_limits: list[tuple] = []
        self.open_orders: list[Any] = []  # OrderState-like, for list_open_orders
        self.open_orders_error: Exception | None = None  # set to fail the book read
        self.order_states: dict[str, Any] = {}  # order_id -> OrderState-like, for get_order
        self._next_id = 0

    def get_account(self) -> Any:
        return _acct()

    def get_positions(self) -> list:
        return []

    def place_bracket_order(self, bracket: Any) -> Any:
        self.brackets.append(bracket)
        return _placed("E-1")

    def place_standalone_stop(self, *a: Any, **k: Any) -> Any:
        self.stops.append((a, k))
        return _placed("S-1")

    def amend_stop_amount(self, *a: Any, **k: Any) -> Any:
        self.amends.append((a, k))
        return type("Placed", (), {"entry_order_id": "", "exit_order_ids": ()})()

    def cancel_order(self, order_id: str) -> None:
        self.cancels.append(order_id)

    def place_trailing_stop(
        self,
        uic: int,
        side: str,
        qty: float,
        order_price: float,
        trailing_distance: float,
        trailing_step: float,
        ceiling_price: float | None = None,
        request_id: str | None = None,
    ) -> Any:
        self._next_id += 1
        order_id = f"TR-{self._next_id}"
        self.trailing_orders.append(
            {
                "uic": uic,
                "side": side,
                "qty": qty,
                "order_price": order_price,
                "trailing_distance": trailing_distance,
                "trailing_step": trailing_step,
                "ceiling_price": ceiling_price,
                "request_id": request_id,
                "order_id": order_id,
            }
        )
        return _placed(order_id)

    def place_stop_limit(self, *a: Any, **k: Any) -> Any:
        self.stop_limits.append((a, k))
        return _placed("SL-1")

    def list_open_orders(self) -> list[Any]:
        if self.open_orders_error is not None:
            raise self.open_orders_error
        return self.open_orders

    def get_order(self, order_id: str) -> Any:
        from broker_contract.contract import BrokerError

        state = self.order_states.get(order_id)
        if state is None:
            raise BrokerError(f"unknown order {order_id}")
        return state


class _FakeFeed:
    """A price feed keyed by uic; a missing/None entry vetoes (returns None).

    Also a ``SupportsSessionLow`` (the 1 Hz touch-latch capability): ``lows``
    holds a per-uic drained sub-tick running low that ``session_low`` POPS
    (drain semantics, mirroring the real feed) so a test can plant a wick for
    exactly one tick. An unset uic drains to ``None`` — the byte-identical
    behaviour the pre-latch feed had for every uic. ``reseed_session_low``
    hands a drained low back (min-merge, mirroring the real accumulator) and
    RECORDS every call in ``reseeds`` so a test can pin exactly which discard
    branches reseed (only the point-veto one) and which stay final."""

    def __init__(
        self,
        prices: dict[int, float | None],
        lows: dict[int, float | None] | None = None,
    ) -> None:
        self._prices = prices
        self._lows = lows if lows is not None else {}
        self.reseeds: list[tuple[int, float]] = []

    def latest(self, uic: int) -> Any:
        price = self._prices.get(uic)
        if price is None:
            return None
        return type("PP", (), {"bid": price, "ask": price})()

    def session_low(self, uic: int) -> float | None:
        return self._lows.pop(uic, None)

    def reseed_session_low(self, uic: int, low: float) -> None:
        self.reseeds.append((uic, low))
        prev = self._lows.get(uic)
        self._lows[uic] = low if prev is None else min(prev, low)


def _journal(test: unittest.TestCase) -> Path:
    d = TemporaryDirectory()
    test.addCleanup(d.cleanup)
    path = Path(d.name) / "entry_trails.jsonl"
    patcher = mock.patch.object(entry_trails, "_entry_trail_journal_path", lambda: path)
    patcher.start()
    test.addCleanup(patcher.stop)
    return path


def _planned_journal(test: unittest.TestCase) -> Path:
    """Point the standalone-stop journal (where the never-naked ``planned``
    disaster-SL line is written at fire-arm) at a temp file so an arming test can
    inspect it AND nothing touches the real ~/.alphalens state."""
    d = TemporaryDirectory()
    test.addCleanup(d.cleanup)
    path = Path(d.name) / "standalone_stops.jsonl"
    patcher = mock.patch.object(cl, "_standalone_stop_journal_path", lambda: path)
    patcher.start()
    test.addCleanup(patcher.stop)
    return path


def _lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _placement(n_tiers: int = 1) -> Any:
    def _bracket(rid: str) -> Any:
        return type(
            "B",
            (),
            {
                "client_request_id": rid,
                "quantity": 1,
                "entry_limit": 10.0,
                "stop_loss": 9.0,
                "take_profit": 12.0,
                "entry_ttl_days": 1,
            },
        )()

    tiers = [
        type("T", (), {"bracket": _bracket(f"rid-{i}"), "tier_index": i, "tp": 12.0})()
        for i in range(n_tiers)
    ]
    return type("P", (), {"tiers": tiers, "disaster_stop_price": 9.0})()


def _placer(test: unittest.TestCase, broker: Any, plan: SetupPlan) -> Any:
    """A ``_place_pick`` closure with the resolve/size/verdict/journal seams
    stubbed hermetic, EXCEPT the entry-trails journal (pointed at a temp file by
    :func:`_journal`). ``classify`` returns a simple placement so the OFF branch
    really reaches ``place_bracket_order`` while the ON branch skips it."""
    pkg = "alphalens_pipeline.brokers"
    submissions: list[dict[str, Any]] = []
    for target, fn in (
        (f"{pkg}.automanager.reconcile_bridge.verdicts", lambda _r, _b, **_k: []),
        (f"{pkg}.automanager.safety.check", lambda *_a, **_k: object()),
        (f"{pkg}.routing.resolve_us_instrument", lambda _b, _t, **_kw: _instr()),
        (f"{pkg}.submission_log.iter_submission_records", lambda _p: []),
        (f"{pkg}.submission_log.build_submission_record", lambda **kw: dict(kw)),
        (f"{pkg}.submission_log.append_submission_record", submissions.append),
        ("broker_contract.sizing.compute_setup_plan", lambda _s, **_k: plan),
        (f"{pkg}.automanager.placement_planner.classify", lambda *_a, **_k: _placement()),
        (f"{pkg}.automanager.picks.mark_refused", lambda *_a, **_k: None),
    ):
        test.enterContext(mock.patch(target, fn))
    test.enterContext(mock.patch.object(cl, "_append_standalone_stop_journal", lambda _l: None))
    placer = cl._make_place_pick(broker)
    return placer, submissions


# --------------------------------------------------------------------------
# Drain intercept (_place_pick) + flag-off byte-identity
# --------------------------------------------------------------------------


class TestDrainInterceptRoutesToWatch(unittest.TestCase):
    def test_flag_on_opens_watch_and_places_no_order(self) -> None:
        path = _journal(self)
        broker = _RecordingBroker()
        placer, submissions = _placer(self, broker, _plan((0, 10.0, 100), (1, 9.0, 100)))
        with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
            self.assertTrue(placer(_pick()))
        # No broker order of ANY kind — the DRY-RUN safety property.
        self.assertEqual(broker.brackets, [])
        self.assertEqual(broker.stops, [])
        # Two watch_open lines (one per positive-qty tier), journal-first.
        watch_opens = [
            line for line in _lines(path) if line["kind"] == entry_trails.KIND_WATCH_OPEN
        ]
        self.assertEqual(len(watch_opens), 2)
        w0 = watch_opens[0]
        self.assertEqual(w0["limit"], 10.0)
        self.assertEqual(w0["qty"], 100.0)
        self.assertEqual(w0["d_bps"], 50)
        self.assertEqual(w0["next_tier_limit"], 9.0)  # deeper tier's limit
        self.assertEqual(w0["uic"], 307)
        self.assertEqual(w0["pick_key"], "KO:2026-07-20")
        self.assertIn("window_end", w0)
        # The pick is retired from the drain via a note-only submission record.
        self.assertEqual(len(submissions), 1)
        self.assertEqual(submissions[0]["brackets"], [])
        self.assertIn("watch", submissions[0]["note"])

    def test_flag_off_is_byte_identical_places_order_and_writes_no_watch(self) -> None:
        path = _journal(self)
        broker = _RecordingBroker()
        placer, _submissions = _placer(self, broker, _plan((0, 10.0, 100)))
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertTrue(placer(_pick()))
        # Normal placement path ran (a bracket was placed) ...
        self.assertEqual(len(broker.brackets), 1)
        # ... and NOTHING was written to the entry-trails journal.
        self.assertEqual(_lines(path), [])

    def test_deepest_tier_has_no_next_tier_limit(self) -> None:
        path = _journal(self)
        broker = _RecordingBroker()
        placer, _s = _placer(self, broker, _plan((0, 10.0, 100), (1, 9.0, 100)))
        with mock.patch.dict("os.environ", {_ENV: "100"}, clear=True):
            placer(_pick())
        watch_opens = [
            line for line in _lines(path) if line["kind"] == entry_trails.KIND_WATCH_OPEN
        ]
        self.assertIsNone(watch_opens[1]["next_tier_limit"])

    def test_capacity_reached_defers_and_opens_no_watch(self) -> None:
        path = _journal(self)
        # Seed a DIFFERENT pick already watching (capacity == 1).
        entry_trails.append_entry_trail_line(
            {
                "kind": entry_trails.KIND_WATCH_OPEN,
                "crid": "OTHER-2026-07-19-entry-t0",
                "limit": 5.0,
                "qty": 10.0,
                "pick_key": "OTHER:2026-07-19",
            }
        )
        broker = _RecordingBroker()
        placer, _s = _placer(self, broker, _plan((0, 10.0, 100)))
        with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
            self.assertFalse(placer(_pick()))  # deferred, stays armed
        self.assertEqual(broker.brackets, [])
        opens_for_ko = [
            line
            for line in _lines(path)
            if line["kind"] == entry_trails.KIND_WATCH_OPEN and line.get("ticker") == "KO"
        ]
        self.assertEqual(opens_for_ko, [])

    def test_own_open_watch_is_exempt_from_capacity_and_retires(self) -> None:
        # Crash recovery: KO's watch_open was journaled but the pick was never
        # retired (crash between the journal-FIRST watch_open and the note-only
        # submission record). On restart the still-armed pick re-runs
        # _place_pick; it must NOT self-block on its OWN reservation (capacity
        # counting its own watch) — it re-opens idempotently and retires.
        path = _journal(self)
        entry_trails.append_entry_trail_line(
            {
                "kind": entry_trails.KIND_WATCH_OPEN,
                "crid": "KO-2026-07-20-entry-t0",
                "limit": 10.0,
                "qty": 100.0,
                "pick_key": "KO:2026-07-20",
            }
        )
        broker = _RecordingBroker()
        placer, submissions = _placer(self, broker, _plan((0, 10.0, 100)))
        with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
            self.assertTrue(placer(_pick()))  # NOT blocked — re-opens + retires
        self.assertEqual(broker.brackets, [])  # still DRY-RUN
        # Retired from the drain (note-only submission record) ...
        self.assertEqual(len(submissions), 1)
        self.assertIn("watch", submissions[0]["note"])
        # ... and the reservation stays a SINGLE watch (deterministic crid,
        # fold latest-wins — no double-count from the re-append).
        total, bad = entry_trails.watching_virtual_gross_acct(entry_trails.read_entry_trail_fold())
        self.assertEqual(bad, 0)
        self.assertEqual(total, 1_000.0)  # 10.0 x 100 once, not doubled

    def test_second_pick_still_blocked_while_first_pick_watches(self) -> None:
        # The capacity gate must still block a DIFFERENT new pick when one pick
        # already watches — the exemption is only for a pick's OWN watch.
        _journal(self)
        entry_trails.append_entry_trail_line(
            {
                "kind": entry_trails.KIND_WATCH_OPEN,
                "crid": "OTHER-2026-07-19-entry-t0",
                "limit": 5.0,
                "qty": 10.0,
                "pick_key": "OTHER:2026-07-19",
            }
        )
        broker = _RecordingBroker()
        placer, submissions = _placer(self, broker, _plan((0, 10.0, 100)))
        with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
            self.assertFalse(placer(_pick()))  # a NEW pick stays blocked
        self.assertEqual(submissions, [])


# --------------------------------------------------------------------------
# The per-tick watcher pass
# --------------------------------------------------------------------------


def _watch_deps(
    feed: Any, alerts: list[tuple[str, str]], broker: Any = None, factory: Any = None
) -> cl.LoopDeps:
    def _throttled(message: str, reason: str) -> bool:
        alerts.append((message, reason))
        return True

    return cl.LoopDeps(
        broker=object() if broker is None else broker,
        kill_file=Path("/nonexistent/KILL"),
        ensure_alive=lambda: type("C", (), {"alive": True, "reason": None})(),  # noqa: PLW0108
        iter_picks=lambda: iter([]),
        place_pick=lambda _p: False,
        read_records=list,
        verdicts_fn=lambda _r, _b, **_k: [],
        build_position_view=lambda _b, _r: object(),
        build_protection_view=lambda _b, _r: object(),
        execute_protection=lambda _a, _k, _r: None,
        sweep_orphans_fn=lambda _b: [],
        alert=lambda _m: None,
        alert_throttled=_throttled,
        live_exits_feed_factory=(factory if factory is not None else (lambda _u2i, *, scope: feed)),
    )


def _seed_watch(
    path: Path,
    *,
    crid: str,
    limit: float,
    next_tier_limit: float | None,
    window_end: str | None = None,
    d_bps: int = 50,
    geometry: dict[str, Any] | None = None,
    qty: float = 100.0,
) -> None:
    line: dict[str, Any] = {
        "kind": entry_trails.KIND_WATCH_OPEN,
        "crid": crid,
        "limit": limit,
        "qty": qty,
        "d_bps": d_bps,
        "window_end": window_end or "2099-01-01T21:00:00+00:00",
        "fx_rate": None,
        "uic": 307,
        "ticker": "KO",
        "exchange_mic": "XNYS",
        "next_tier_limit": next_tier_limit,
        "pick_key": "KO:2026-07-20",
        "entry_mode": "entry-trail-native-d50-testcfg",
        "disaster_stop": 8.0,
        "tier_index": 0,
    }
    if geometry is not None:
        # The router stamps this blob on the watch_open line at routing time
        # (control_loop._open_entry_watches); it carries the exit target the
        # live exit engine fires on.
        line["geometry"] = geometry
    entry_trails.append_entry_trail_line(line)


def _seed_tranche_plan(
    *,
    uic: int,
    tranche_fracs: tuple[float, ...],
    reference_qty: float,
    targets: tuple[float, ...] | None = None,
) -> None:
    """Journal one ``tranche_plan`` line for ``uic``, the shape the router writes
    before it opens the watches. ``tranche_fracs`` is the exit split: ``(1.0,)``
    is the single whole-position tranche the geometry policy produces;
    ``targets`` overrides the per-tranche target prices (default 100.0 + i, far
    above every seeded touch, so only a test about the BRIEF-ladder cost gate
    needs to pass its own)."""
    from broker_contract.sizing import TpTranchePlan

    cl._append_standalone_stop_journal(
        cl._build_tranche_plan_line(
            uic=uic,
            tp_tranches=tuple(
                TpTranchePlan(
                    tranche_index=i,
                    target_price=targets[i] if targets is not None else 100.0 + i,
                    tranche_frac=pct,
                    r_multiple=0.0,
                    tag="geometry" if len(tranche_fracs) == 1 else f"tp{i + 1}",
                )
                for i, pct in enumerate(tranche_fracs)
            ),
            reference_qty=reference_qty,
            stop_price=8.0,
            pick_key="KO:2026-07-20",
        )
    )


def _seed_healthy_plan(*, reference_qty: float = 100.0) -> None:
    """A journaled brief-thirds plan whose default targets (100.0+) sit far
    above every seeded touch — for tests whose subject is NOT one of the #1112
    arm gates. Production writes a ``tranche_plan`` before every ``watch_open``
    (pinned by ``TestWatchRoutingJournalsTranchePlan``), so a seeded watch with
    no plan at all exercises a state the router cannot produce — and the
    brief-ladder arm gate fails CLOSED on it by design."""
    _seed_tranche_plan(uic=307, tranche_fracs=(1 / 3, 1 / 3, 1 / 3), reference_qty=reference_qty)


_ALLOW = {_ENV: "50", entry_trails.ENTRY_TRAIL_BPS_ENV: "50", "ALPHALENS_BROKER_ALLOW_ORDERS": "1"}


class TestEntryWatchPassNativeArm(unittest.TestCase):
    """PR-T2b: the executor PLACES one native trailing-LIMIT order at TOUCH (the
    server ratchets + fires) instead of the dry-run would-fire. No resting-limit
    bracket, no fabricated fired line."""

    def _run(
        self,
        deps: cl.LoopDeps,
        price: float | None,
        feed: dict[int, float | None],
        env: dict[str, str] | None = None,
    ) -> None:
        feed[307] = price
        with mock.patch.dict("os.environ", env or _ALLOW, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())

    def test_touch_places_exactly_one_native_trailing_order_no_resting_limit(self) -> None:
        path = _journal(self)
        planned_path = _planned_journal(self)
        _seed_healthy_plan()
        _seed_watch(path, crid="KO-2026-07-20-entry-t0", limit=10.0, next_tier_limit=None)
        prices: dict[int, float | None] = {}
        broker = _RecordingBroker()
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)

        self._run(deps, 10.0, prices)  # touch @ limit -> ARM

        # Exactly ONE native trailing order — and NO resting-limit / bracket /
        # standalone-stop entry order.
        self.assertEqual(len(broker.trailing_orders), 1)
        self.assertEqual(broker.brackets, [])
        self.assertEqual(broker.stops, [])
        order = broker.trailing_orders[0]
        self.assertEqual(order["side"], "BUY")
        self.assertEqual(order["qty"], 100.0)
        self.assertEqual(order["request_id"], "KO-2026-07-20-entry-t0-fire")
        # The combined trailing-LIMIT carries the G1 ceiling and an initial trigger.
        self.assertIsNotNone(order["ceiling_price"])
        self.assertGreaterEqual(order["ceiling_price"], order["order_price"])
        self.assertGreater(order["order_price"], 10.0)  # trigger sits above the bid

        # trail_armed line filled with the REAL order id (G3: null write-ahead
        # first, then the real-id line — the fold's latest wins).
        armed = [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_TRAIL_ARMED]
        self.assertEqual(armed[-1]["order_id"], "TR-1")
        # NO fabricated fired line (native mode: the server is the fire event).
        self.assertEqual([ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_FIRED], [])

        # NEVER-NAKED: the planned disaster-SL line was written at FIRE-ARM, keyed
        # to the resting order's ExternalReference, so a fill is covered by the
        # existing protection pass.
        planned = [json.loads(x) for x in planned_path.read_text().splitlines() if x]
        planned = [p for p in planned if p.get("kind") == "planned"]
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["side"], "SELL")
        self.assertEqual(planned[0]["stop_price"], 8.0)
        self.assertEqual(planned[0]["client_request_id"], "KO-2026-07-20-entry-t0-fire")

    def test_armed_tier_is_excluded_next_tick_and_never_re_places(self) -> None:
        path = _journal(self)
        _planned_journal(self)
        _seed_healthy_plan()
        _seed_watch(path, crid="KO-2026-07-20-entry-t0", limit=10.0, next_tier_limit=None)
        prices: dict[int, float | None] = {}
        broker = _RecordingBroker()
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)
        self._run(deps, 10.0, prices)  # touch -> arm (order TR-1)
        self._run(deps, 9.80, prices)  # armed tier is resting -> no re-arm
        self._run(deps, 9.95, prices)  # still resting
        self.assertEqual(len(broker.trailing_orders), 1, "the resting order is armed exactly once")

    def test_arm_in_progress_null_id_adopts_the_working_order_no_double_place(self) -> None:
        # G3 crash recovery: a trail_armed write-ahead line with a NULL id (POST
        # done, id-journal lost) + the real order still resting at the broker.
        # On the next TOUCHED tick the executor ADOPTS it (by ExternalReference),
        # never resting a second trail.
        path = _journal(self)
        _planned_journal(self)
        _seed_watch(path, crid="KO-2026-07-20-entry-t0", limit=10.0, next_tier_limit=None)
        entry_trails.append_entry_trail_line(
            {
                "kind": entry_trails.KIND_TRAIL_ARMED,
                "crid": "KO-2026-07-20-entry-t0",
                "order_id": None,
            }
        )
        broker = _RecordingBroker()
        broker.open_orders = [
            type(
                "OS",
                (),
                {"order_id": "TR-EXISTING", "external_reference": "KO-2026-07-20-entry-t0-fire"},
            )()
        ]
        prices: dict[int, float | None] = {}
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)
        self._run(deps, 9.95, prices)  # resume arm-in-progress -> adopt, no POST
        self.assertEqual(broker.trailing_orders, [], "adopting must not POST a second order")
        armed = [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_TRAIL_ARMED]
        self.assertEqual(armed[-1]["order_id"], "TR-EXISTING")

    def test_allow_orders_off_places_nothing_and_writes_no_write_ahead(self) -> None:
        path = _journal(self)
        _planned_journal(self)
        _seed_watch(path, crid="KO-2026-07-20-entry-t0", limit=10.0, next_tier_limit=None)
        prices: dict[int, float | None] = {}
        broker = _RecordingBroker()
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)
        # Flag ON but ALLOW_ORDERS off: no POST, no write-ahead trail_armed line.
        self._run(deps, 10.0, prices, env={_ENV: "50"})
        self.assertEqual(broker.trailing_orders, [])
        self.assertEqual(
            [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_TRAIL_ARMED], []
        )

    def test_insufficient_funds_at_arm_refuses_the_tier_terminal(self) -> None:
        from broker_contract.contract import OrderRejectedError

        path = _journal(self)
        _planned_journal(self)
        _seed_healthy_plan()
        _seed_watch(path, crid="KO-2026-07-20-entry-t0", limit=10.0, next_tier_limit=None)
        prices: dict[int, float | None] = {}
        broker = _RecordingBroker()

        def _reject(*_a: Any, **_k: Any) -> Any:
            raise OrderRejectedError("no cash", error_code="InsufficientCash")

        broker.place_trailing_stop = _reject  # type: ignore[method-assign]
        alerts: list[tuple[str, str]] = []
        deps = _watch_deps(_FakeFeed(prices), alerts, broker=broker)
        self._run(deps, 10.0, prices)
        cancelled = [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_CANCELLED]
        self.assertEqual(len(cancelled), 1, "insufficient funds terminal-refuses the tier (G7)")

    def test_deep_decline_suspends_on_a_single_gap_down_tick_no_order(self) -> None:
        # A gap-down tick that touches AND is already below the next tier suspends
        # ON the touch tick (state=SUSPENDED, not TOUCHED) — so the executor never
        # arms. No order placed.
        path = _journal(self)
        _planned_journal(self)
        _seed_watch(path, crid="KO-2026-07-20-entry-t0", limit=10.0, next_tier_limit=9.5)
        prices: dict[int, float | None] = {}
        broker = _RecordingBroker()
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)
        self._run(deps, 9.40, prices)  # touch + below next tier 9.5 -> suspend
        self.assertEqual(broker.trailing_orders, [])
        suspended = [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_SUSPENDED]
        self.assertEqual(len(suspended), 1)

    def test_ttl_expiry_terminates_even_without_a_fresh_price(self) -> None:
        path = _journal(self)
        _seed_watch(
            path,
            crid="KO-2026-07-20-entry-t0",
            limit=10.0,
            next_tier_limit=None,
            window_end="2000-01-01T00:00:00+00:00",  # already past
        )
        prices: dict[int, float | None] = {307: None}  # no price this tick
        broker = _RecordingBroker()
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)
        with mock.patch.dict("os.environ", _ALLOW, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())
        expired = [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_EXPIRED]
        self.assertEqual(len(expired), 1)
        self.assertEqual(broker.trailing_orders, [])

    def test_non_trailing_broker_never_gets_an_order_call(self) -> None:
        # A broker without SupportsTrailingStop degrades safely: the arm is a
        # no-op (the drain intercept would not have routed to a watch either).
        path = _journal(self)
        _seed_watch(path, crid="KO-2026-07-20-entry-t0", limit=10.0, next_tier_limit=None)
        prices: dict[int, float | None] = {}
        deps = _watch_deps(_FakeFeed(prices), [])  # broker=object()
        with mock.patch.dict("os.environ", _ALLOW, clear=True):
            for price in (10.0, 9.90, 9.95):
                prices[307] = price
                cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())
        # No exception (no order method called) and no trail_armed line.
        self.assertEqual(
            [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_TRAIL_ARMED], []
        )


class TestEntryArmInsideExitRegion(unittest.TestCase):
    """Issue #1112 step 1: a tier whose realistic fill would land AT OR ABOVE the
    exit target already stamped on its own watch must NOT arm.

    LIVE 2026-08-24 (SMG): tier limit 59.786017 sat above the geometry target
    59.6277, so the fill at 59.9261 was past its take-profit the moment it
    happened and the exit engine sold 62 seconds later for about -380 bps.
    """

    def _run(
        self,
        deps: cl.LoopDeps,
        price: float | None,
        feed: dict[int, float | None],
    ) -> None:
        feed[307] = price
        with mock.patch.dict("os.environ", _ALLOW, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())

    def _seed(
        self,
        path: Path,
        *,
        limit: float,
        geometry: dict[str, Any] | None,
        qty: float = 100.0,
        tranche_fracs: tuple[float, ...] = (1.0,),
    ) -> None:
        # Production journals the uic's tranche_plan BEFORE the watch_open lines
        # (control_loop._route_pick_to_entry_watch), so every seeded watch gets
        # one too. ``tranche_fracs`` defaults to the single 100% geometry tranche
        # the live rail writes; a test that wants the multi-tranche shape passes
        # its own split.
        _seed_tranche_plan(uic=307, tranche_fracs=tranche_fracs, reference_qty=qty)
        _seed_watch(
            path,
            crid="KO-2026-07-20-entry-t0",
            limit=limit,
            next_tier_limit=None,
            geometry=geometry,
            qty=qty,
        )

    def _arm_count(self, *, limit: float, target: float, touch: float, qty: float) -> int:
        """How many native trails one touch tick places for a tier with this
        stamped exit target — the whole ``_run_entry_watch_pass`` path, not the
        predicate."""
        path = _journal(self)
        _planned_journal(self)
        self._seed(
            path,
            limit=limit,
            geometry={**smg_geometry_stamp(), "geometry_tp": target},
            qty=qty,
        )
        prices: dict[int, float | None] = {}
        broker = _RecordingBroker()
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)
        self._run(deps, touch, prices)
        return len(broker.trailing_orders)

    def test_the_gate_uses_the_realistic_fill_estimate_not_the_nominal_tier_limit(self) -> None:
        # The issue's "a build-time check on the nominal limit is not
        # sufficient", pinned END TO END on the LIVE rail size (1 share).
        #
        #   tier limit 59.786017 -> minimum profitable exit 62.3839
        #   fill estimate 60.1890 -> minimum profitable exit 62.7909
        #
        # A target of 62.60 sits BETWEEN them: a gate reading the nominal limit
        # arms this tier, a gate reading the realistic fill refuses it. That
        # band is exactly the defect class the 2026-08-24 fill (23 bps above its
        # own limit) fell into.
        self.assertEqual(
            self._arm_count(limit=SMG_TIERS[0][0], target=62.60, touch=SMG_TOUCH_BID, qty=1.0),
            0,
            "a target the realistic fill cannot clear must refuse the arm",
        )
        # Just above the estimate-based threshold the same tier still arms, so
        # the assertion above is a real discriminator, not a blanket refusal.
        self.assertEqual(
            self._arm_count(limit=SMG_TIERS[0][0], target=62.90, touch=SMG_TOUCH_BID, qty=1.0),
            1,
            "a target that clears cost plus the buffer must still arm",
        )

    def test_top_tier_inside_the_exit_region_places_no_order_and_terminates(self) -> None:
        path = _journal(self)
        _planned_journal(self)
        self._seed(path, limit=SMG_TIERS[0][0], geometry=smg_geometry_stamp())
        prices: dict[int, float | None] = {}
        broker = _RecordingBroker()
        alerts: list[tuple[str, str]] = []
        deps = _watch_deps(_FakeFeed(prices), alerts, broker=broker)

        self._run(deps, SMG_TOUCH_BID, prices)  # the real touch tick

        self.assertEqual(broker.trailing_orders, [], "no native trail may be armed")
        self.assertEqual(broker.brackets, [])
        self.assertEqual(broker.stops, [])
        self.assertEqual(
            [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_TRAIL_ARMED], []
        )
        cancelled = [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_CANCELLED]
        self.assertEqual(len(cancelled), 1, "the tier is terminal-refused, not retried forever")
        self.assertIn("exit region", cancelled[0]["note"])
        self.assertTrue(any("exit region" in msg for msg, _key in alerts))

    def test_refused_tier_does_not_retry_on_the_next_tick(self) -> None:
        path = _journal(self)
        _planned_journal(self)
        self._seed(path, limit=SMG_TIERS[0][0], geometry=smg_geometry_stamp())
        prices: dict[int, float | None] = {}
        broker = _RecordingBroker()
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)
        for price in (SMG_TOUCH_BID, SMG_TOUCH_BID - 0.02, SMG_TOUCH_BID - 0.01):
            self._run(deps, price, prices)
        self.assertEqual(broker.trailing_orders, [])
        cancelled = [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_CANCELLED]
        self.assertEqual(len(cancelled), 1, "terminal-refused exactly once, then dropped")

    def test_healthy_live_tiers_still_arm(self) -> None:
        # Regression against the same live journal: SMG E2 / SMG E3 / ETSY E3.
        for label, limit, geometry in (
            ("SMG E2", SMG_TIERS[1][0], smg_geometry_stamp()),
            ("SMG E3", SMG_TIERS[2][0], smg_geometry_stamp()),
            ("ETSY E3", ETSY_E3_LIMIT, {**smg_geometry_stamp(), "geometry_tp": ETSY_E3_TARGET}),
        ):
            with self.subTest(tier=label):
                path = _journal(self)
                _planned_journal(self)
                self._seed(path, limit=limit, geometry=geometry)
                prices: dict[int, float | None] = {}
                broker = _RecordingBroker()
                deps = _watch_deps(_FakeFeed(prices), [], broker=broker)
                self._run(deps, limit, prices)
                self.assertEqual(len(broker.trailing_orders), 1, f"{label} must still arm")

    def test_an_already_resting_order_is_adopted_never_orphaned(self) -> None:
        # G3 crash recovery on a tier the gate would refuse: the order ALREADY
        # rests at the broker. Refusing here would terminate the watch while
        # leaving a live buy order nobody tracks (KIND_CANCELLED is outside
        # _RESTING_BEARING_TERMINALS, so no cancel-then-verify runs). Adopt it,
        # exactly as before the gate existed.
        path = _journal(self)
        _planned_journal(self)
        self._seed(path, limit=SMG_TIERS[0][0], geometry=smg_geometry_stamp())
        broker = _RecordingBroker()
        broker.open_orders = [
            type(
                "OS",
                (),
                {"order_id": "TR-RESTING", "external_reference": "KO-2026-07-20-entry-t0-fire"},
            )()
        ]
        prices: dict[int, float | None] = {}
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)
        self._run(deps, SMG_TOUCH_BID, prices)
        armed = [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_TRAIL_ARMED]
        self.assertEqual([ln["order_id"] for ln in armed], ["TR-RESTING"])
        self.assertEqual(
            [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_CANCELLED], []
        )
        self.assertEqual(broker.trailing_orders, [], "adopted, not re-placed")

    def test_a_failed_open_order_read_defers_the_refusal_instead_of_orphaning(self) -> None:
        # The adopt read is what proves no order of ours rests at the broker, and
        # it returns "not found" both when the book is empty AND when the read
        # itself failed. Terminal-refusing on a FAILED read would drop a watch
        # whose prior-tick POST may be resting, with nothing left to cancel it
        # (KIND_CANCELLED is outside _RESTING_BEARING_TERMINALS). The tier stays
        # TOUCHED and retries on the next tick instead.
        from broker_contract.contract import BrokerError

        path = _journal(self)
        _planned_journal(self)
        self._seed(path, limit=SMG_TIERS[0][0], geometry=smg_geometry_stamp())
        broker = _RecordingBroker()
        broker.open_orders_error = BrokerError("book read failed")
        prices: dict[int, float | None] = {}
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)

        self._run(deps, SMG_TOUCH_BID, prices)

        self.assertEqual(
            [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_CANCELLED],
            [],
            "a refusal on an unreadable book would strand a possibly-resting order",
        )
        self.assertEqual(broker.trailing_orders, [], "and it must not arm the bad tier either")
        # The next tick, with the book readable again, refuses properly.
        broker.open_orders_error = None
        self._run(deps, SMG_TOUCH_BID, prices)
        self.assertEqual(
            len([ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_CANCELLED]), 1
        )

    def test_watch_without_a_geometry_stamp_still_arms(self) -> None:
        # Fail open: a pre-stamp watch_open line (or a policy that places the
        # brief's own ladder) carries no target to compare against.
        path = _journal(self)
        _planned_journal(self)
        self._seed(path, limit=SMG_TIERS[0][0], geometry=None)
        prices: dict[int, float | None] = {}
        broker = _RecordingBroker()
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)
        self._run(deps, SMG_TOUCH_BID, prices)
        self.assertEqual(len(broker.trailing_orders), 1)

    def test_degenerate_or_unapplied_geometry_stamp_still_arms(self) -> None:
        for label, stamp in (
            ("tp None", {**smg_geometry_stamp(), "geometry_tp": None}),
            ("tp NaN", {**smg_geometry_stamp(), "geometry_tp": float("nan")}),
            ("tp zero", {**smg_geometry_stamp(), "geometry_tp": 0.0}),
            ("tp not a number", {**smg_geometry_stamp(), "geometry_tp": "x"}),
            ("not applied", {**smg_geometry_stamp(), "applied": False}),
            ("stamp not a mapping", None),
        ):
            with self.subTest(case=label):
                path = _journal(self)
                _planned_journal(self)
                self._seed(path, limit=SMG_TIERS[0][0], geometry=stamp)
                prices: dict[int, float | None] = {}
                broker = _RecordingBroker()
                deps = _watch_deps(_FakeFeed(prices), [], broker=broker)
                self._run(deps, SMG_TOUCH_BID, prices)
                self.assertEqual(len(broker.trailing_orders), 1, f"{label} must fail open")


class TestGeometryActiveWithTheTrailDisabled(unittest.TestCase):
    """#1116 round 2, point 4: the #1112 exit-region arm gate lives ONLY on the
    trailing-entry path. With ``ALPHALENS_BROKER_ENTRY_TRAIL_BPS`` at 0 a pick
    falls through to the classic ``_place_tiers`` bracket path, which has no such
    gate — and 0 is what the repo's own systemd unit sets (issue #1121).

    Fail closed on ARMING A NEW ENTRY, never on the daemon as a whole: refusing
    to start would leave live positions unmanaged, which is far worse than the
    defect. Positions already open keep being managed — pinned in
    ``test_live_exits_pass.py``.
    """

    _TIER_LIMIT = 10.0
    _GEOMETRY_STOP = 8.0
    _GEOMETRY_TP = 12.0

    def _geometry_pick(self) -> Any:
        """A pick carrying a buildable ``exit`` spec, which is the second half of
        ``use_geometry`` (the first is the policy's ``applies_geometry``)."""
        pick = _pick()
        pick.spec = type(
            "Spec",
            (),
            {
                "entry_tiers": (
                    type("T", (), {"limit_price": self._TIER_LIMIT, "alloc_pct": 100.0})(),
                )
            },
        )()
        pick.exit = type(
            "ExitSpec",
            (),
            {
                "initial_levels": type(
                    "L", (), {"stop": self._GEOMETRY_STOP, "tp": self._GEOMETRY_TP}
                )(),
                "reaction_plan": (),
            },
        )()
        return pick

    def _place(
        self, *, trail_bps: str | None, geometry: bool
    ) -> tuple[_RecordingBroker, bool, list[tuple[str, str]]]:
        from broker_contract.exit_geometry.registry import resolve_exit_policy

        _journal(self)
        broker = _RecordingBroker()
        _placer(self, broker, _plan((0, self._TIER_LIMIT, 100)))
        alerts: list[tuple[str, str]] = []

        def _throttled(message: str, reason: str) -> bool:
            alerts.append((message, reason))
            return True

        placer = cl._make_place_pick(
            broker,
            resolve_exit_policy("atr_bracket_1p5" if geometry else "setup_static"),
            alert_throttled=_throttled,
        )
        env = {} if trail_bps is None else {_ENV: trail_bps}
        with mock.patch.dict("os.environ", env, clear=True):
            placed = placer(self._geometry_pick())
        return broker, placed, alerts

    def test_geometry_active_and_the_trail_off_refuses_the_new_entry(self) -> None:
        broker, placed, alerts = self._place(trail_bps=None, geometry=True)
        self.assertFalse(placed)
        self.assertEqual(broker.brackets, [], "no un-gated bracket may be placed")
        self.assertEqual(broker.stops, [])
        self.assertTrue(any("entry trail" in msg for msg, _key in alerts), alerts)

    def test_an_explicit_zero_is_the_same_as_unset(self) -> None:
        broker, placed, _alerts = self._place(trail_bps="0", geometry=True)
        self.assertFalse(placed)
        self.assertEqual(broker.brackets, [])

    def test_the_classic_path_is_untouched_when_geometry_is_not_active(self) -> None:
        broker, placed, alerts = self._place(trail_bps=None, geometry=False)
        self.assertTrue(placed)
        self.assertEqual(len(broker.brackets), 1, "the static-exit bracket path is unchanged")
        self.assertEqual(alerts, [])

    def test_geometry_active_with_the_trail_on_still_routes_to_a_watch(self) -> None:
        broker, placed, _alerts = self._place(trail_bps="50", geometry=True)
        self.assertTrue(placed)
        self.assertEqual(broker.brackets, [], "the gated trailing path places no bracket")


class TestEntryArmSingleTrancheContract(unittest.TestCase):
    """#1116 round 2, point 2: the arm gate prices the round trip at the whole
    position it opens. That is only safe while the exit side sells that whole
    position in ONE tranche, which is what the live rail does today and what
    nothing in the code enforced.

    A healthy target is used throughout (well clear of the cost gate) so the
    only thing under test is the shape of the exit plan.
    """

    _HEALTHY_TARGET = 90.0
    """Far above the fill estimate for a 59.79 touch, so the #1112 exit-region
    gate never fires and cannot be mistaken for this one."""

    _POSITION_QTY = 27.0
    """The live 27-share position (uic 23474)."""

    def _run_touch(
        self, *, tranche_fracs: tuple[float, ...]
    ) -> tuple[_RecordingBroker, list[dict[str, Any]], list[tuple[str, str]]]:
        path = _journal(self)
        _planned_journal(self)
        _seed_tranche_plan(uic=307, tranche_fracs=tranche_fracs, reference_qty=self._POSITION_QTY)
        _seed_watch(
            path,
            crid="KO-2026-07-20-entry-t0",
            limit=SMG_TIERS[0][0],
            next_tier_limit=None,
            geometry={**smg_geometry_stamp(), "geometry_tp": self._HEALTHY_TARGET},
            qty=self._POSITION_QTY,
        )
        prices: dict[int, float | None] = {}
        broker = _RecordingBroker()
        alerts: list[tuple[str, str]] = []
        deps = _watch_deps(_FakeFeed(prices), alerts, broker=broker)
        prices[307] = SMG_TOUCH_BID
        with mock.patch.dict("os.environ", _ALLOW, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())
        return broker, _lines(path), alerts

    def test_todays_single_full_position_tranche_arms_normally(self) -> None:
        broker, lines, alerts = self._run_touch(tranche_fracs=(1.0,))
        self.assertEqual(len(broker.trailing_orders), 1)
        self.assertEqual([ln for ln in lines if ln["kind"] == entry_trails.KIND_CANCELLED], [])
        self.assertEqual(alerts, [])

    def test_a_restored_three_tranche_plan_refuses_the_arm_and_alerts(self) -> None:
        broker, lines, alerts = self._run_touch(tranche_fracs=(0.33, 0.33, 0.34))
        self.assertEqual(broker.trailing_orders, [], "no order may be placed on an unchecked plan")
        cancelled = [ln for ln in lines if ln["kind"] == entry_trails.KIND_CANCELLED]
        self.assertEqual(len(cancelled), 1)
        self.assertIn("exit plan", cancelled[0]["note"])
        self.assertTrue(any("exit plan" in msg for msg, _key in alerts), alerts)

    def test_a_tranche_that_sells_only_part_of_the_position_refuses_the_arm(self) -> None:
        broker, lines, _alerts = self._run_touch(tranche_fracs=(0.5,))
        self.assertEqual(broker.trailing_orders, [])
        self.assertEqual(len([ln for ln in lines if ln["kind"] == entry_trails.KIND_CANCELLED]), 1)

    def test_a_uic_with_no_tranche_plan_on_record_refuses_the_arm(self) -> None:
        # Fail closed, not open: the router writes the plan BEFORE the watch, so
        # a missing plan means the exit shape this gate depends on is unknown.
        path = _journal(self)
        _planned_journal(self)
        _seed_watch(
            path,
            crid="KO-2026-07-20-entry-t0",
            limit=SMG_TIERS[0][0],
            next_tier_limit=None,
            geometry={**smg_geometry_stamp(), "geometry_tp": self._HEALTHY_TARGET},
            qty=self._POSITION_QTY,
        )
        prices: dict[int, float | None] = {}
        broker = _RecordingBroker()
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)
        prices[307] = SMG_TOUCH_BID
        with mock.patch.dict("os.environ", _ALLOW, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())
        self.assertEqual(broker.trailing_orders, [])
        self.assertEqual(
            len([ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_CANCELLED]), 1
        )

    def test_an_unreadable_plan_journal_defers_instead_of_terminating(self) -> None:
        # The shape check reads the standalone-stop journal, and it runs inside
        # _run_entry_watch_pass, which has no per-watch exception boundary. An
        # OSError there must not abort the pass for every other watch, and must
        # not terminate this tier either — a transient read failure is not
        # evidence about the exit plan. Stay TOUCHED and settle on a later tick.
        path = _journal(self)
        _planned_journal(self)
        _seed_tranche_plan(uic=307, tranche_fracs=(1.0,), reference_qty=self._POSITION_QTY)
        _seed_watch(
            path,
            crid="KO-2026-07-20-entry-t0",
            limit=SMG_TIERS[0][0],
            next_tier_limit=None,
            geometry={**smg_geometry_stamp(), "geometry_tp": self._HEALTHY_TARGET},
            qty=self._POSITION_QTY,
        )
        prices: dict[int, float | None] = {}
        broker = _RecordingBroker()
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)
        prices[307] = SMG_TOUCH_BID

        def _boom():
            raise OSError("journal unreadable")

        with (
            mock.patch.dict("os.environ", _ALLOW, clear=True),
            mock.patch.object(cl, "_iter_standalone_stop_journal", _boom),
        ):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())

        self.assertEqual(broker.trailing_orders, [], "must not arm on an unverified plan")
        self.assertEqual(
            [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_CANCELLED],
            [],
            "a transient read failure is not evidence the plan is wrong",
        )

    def test_a_healthy_brief_ladder_without_a_geometry_target_still_arms(self) -> None:
        # A watch with NO applied geometry target is outside THIS contract (the
        # single-full-tranche shape protects whole-position pricing, which only
        # the geometry path does). It is NOT unguarded: the brief-ladder gate
        # (TestBriefLadderArmGate below) prices it against its own tp1. Here the
        # seeded targets sit far above the touch, so the healthy plan must arm.
        path = _journal(self)
        _planned_journal(self)
        _seed_tranche_plan(uic=307, tranche_fracs=(0.33, 0.33, 0.34), reference_qty=100.0)
        _seed_watch(
            path,
            crid="KO-2026-07-20-entry-t0",
            limit=SMG_TIERS[0][0],
            next_tier_limit=None,
            geometry=None,
        )
        prices: dict[int, float | None] = {}
        broker = _RecordingBroker()
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)
        prices[307] = SMG_TOUCH_BID
        with mock.patch.dict("os.environ", _ALLOW, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())
        self.assertEqual(len(broker.trailing_orders), 1)


class TestBriefLadderArmGate(unittest.TestCase):
    """Issue #1112, the breakeven_trail follow-up: since #1183 the placed exit is
    the BRIEF's own TP ladder (``applies_geometry=False``), and the geometry-
    scoped arm gate above prices nothing on those watches. The gate must instead
    price the brief's FIRST take-profit tranche — at that tranche's APPORTIONED
    share count — against the realistic fill estimate plus round-trip cost and
    the declared buffer, and it must fail CLOSED on a plan it cannot read the
    shape of (missing plan, partial coverage).

    Numbers reuse the measured SMG band: touch 59.77 at 50 bps trail gives a
    fill estimate of 60.1890, whose one-share cost bar is 62.7909 — so a tp1 of
    62.60 is INSIDE the band (all reward goes to the broker) and 62.90 clears.
    """

    _BREAKEVEN_TRAIL_STAMP = {**smg_geometry_stamp(), "applied": False}
    """The shadow stamp the router writes under a no-geometry policy: the blob
    rides along for telemetry, but ``applied`` is False and the placed exit is
    the brief ladder."""

    _THIRDS = (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0)

    def _run_touch(
        self,
        *,
        tp1_target: float,
        reference_qty: float,
        watch_qty: float,
        tranche_fracs: tuple[float, ...] | None = None,
        seed_plan: bool = True,
        stamp: bool = True,
    ) -> tuple[_RecordingBroker, list[dict[str, Any]], list[tuple[str, str]]]:
        path = _journal(self)
        _planned_journal(self)
        fracs = tranche_fracs if tranche_fracs is not None else self._THIRDS
        if seed_plan:
            _seed_tranche_plan(
                uic=307,
                tranche_fracs=fracs,
                reference_qty=reference_qty,
                targets=tuple(tp1_target + 5.0 * i for i in range(len(fracs))),
            )
        _seed_watch(
            path,
            crid="KO-2026-07-20-entry-t0",
            limit=SMG_TIERS[0][0],
            next_tier_limit=None,
            geometry=self._BREAKEVEN_TRAIL_STAMP if stamp else None,
            qty=watch_qty,
        )
        prices: dict[int, float | None] = {307: SMG_TOUCH_BID}
        broker = _RecordingBroker()
        alerts: list[tuple[str, str]] = []
        deps = _watch_deps(_FakeFeed(prices), alerts, broker=broker)
        with mock.patch.dict("os.environ", _ALLOW, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())
        return broker, _lines(path), alerts

    def test_tp1_inside_the_cost_band_refuses_terminally_and_alerts(self) -> None:
        broker, lines, alerts = self._run_touch(tp1_target=62.60, reference_qty=1.0, watch_qty=1.0)
        self.assertEqual(broker.trailing_orders, [], "no native trail may be armed")
        cancelled = [ln for ln in lines if ln["kind"] == entry_trails.KIND_CANCELLED]
        self.assertEqual(len(cancelled), 1, "the tier is terminal-refused, not retried forever")
        self.assertIn("exit region", cancelled[0]["note"])
        self.assertTrue(any("exit region" in msg for msg, _key in alerts), alerts)

    def test_a_tp1_that_clears_the_cost_band_still_arms(self) -> None:
        broker, lines, _alerts = self._run_touch(tp1_target=62.90, reference_qty=1.0, watch_qty=1.0)
        self.assertEqual(len(broker.trailing_orders), 1, "the refusal must discriminate")
        self.assertEqual([ln for ln in lines if ln["kind"] == entry_trails.KIND_CANCELLED], [])

    def test_the_gate_prices_the_first_tranche_apportioned_quantity(self) -> None:
        # tp1 61.50 sits ABOVE the 10-share bar (~60.99) and INSIDE the 1-share
        # bar (62.79). A 30-share plan apportions 10 shares to tp1, so it arms;
        # a 1-share plan prices 1 share and is refused. Pricing the WATCH
        # quantity instead (1 share in both cases) would refuse both.
        broker, _, _alerts = self._run_touch(tp1_target=61.50, reference_qty=30.0, watch_qty=1.0)
        self.assertEqual(len(broker.trailing_orders), 1, "10-share tp1 clears its own bar")
        broker, lines, _alerts = self._run_touch(tp1_target=61.50, reference_qty=1.0, watch_qty=1.0)
        self.assertEqual(broker.trailing_orders, [], "1-share tp1 does not clear its bar")
        self.assertEqual(len([ln for ln in lines if ln["kind"] == entry_trails.KIND_CANCELLED]), 1)

    def test_a_missing_tranche_plan_refuses_terminally(self) -> None:
        # Fail closed: the router writes the plan BEFORE the watch, so a missing
        # plan means the exit shape is unknown — same stance as the geometry
        # path's shape contract.
        broker, lines, _alerts = self._run_touch(
            tp1_target=90.0, reference_qty=1.0, watch_qty=1.0, seed_plan=False
        )
        self.assertEqual(broker.trailing_orders, [])
        cancelled = [ln for ln in lines if ln["kind"] == entry_trails.KIND_CANCELLED]
        self.assertEqual(len(cancelled), 1)
        self.assertIn("no exit plan", cancelled[0]["note"])

    def test_a_partial_coverage_plan_refuses_terminally(self) -> None:
        # A plan whose apportioned tranches sell only half the position leaves
        # the rest permanently stop-only — the exact defect class this gate
        # exists for, declared instead of accidental.
        broker, lines, _alerts = self._run_touch(
            tp1_target=90.0, reference_qty=8.0, watch_qty=8.0, tranche_fracs=(0.25, 0.25)
        )
        self.assertEqual(broker.trailing_orders, [])
        cancelled = [ln for ln in lines if ln["kind"] == entry_trails.KIND_CANCELLED]
        self.assertEqual(len(cancelled), 1)
        self.assertIn("exit plan", cancelled[0]["note"])

    def test_an_unreadable_plan_journal_defers_instead_of_terminating(self) -> None:
        # Same stance as the geometry path: a read failure is not evidence about
        # the plan. No arm, no terminal line — the tier stays TOUCHED.
        path = _journal(self)
        _planned_journal(self)
        _seed_tranche_plan(uic=307, tranche_fracs=self._THIRDS, reference_qty=1.0)
        _seed_watch(
            path,
            crid="KO-2026-07-20-entry-t0",
            limit=SMG_TIERS[0][0],
            next_tier_limit=None,
            geometry=self._BREAKEVEN_TRAIL_STAMP,
            qty=1.0,
        )
        prices: dict[int, float | None] = {307: SMG_TOUCH_BID}
        broker = _RecordingBroker()
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)

        def _boom():
            raise OSError("journal unreadable")

        with (
            mock.patch.dict("os.environ", _ALLOW, clear=True),
            mock.patch.object(cl, "_iter_standalone_stop_journal", _boom),
        ):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())

        self.assertEqual(broker.trailing_orders, [], "must not arm on an unverified plan")
        self.assertEqual(
            [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_CANCELLED], []
        )

    def test_a_watch_with_no_stamp_at_all_is_gated_the_same_way(self) -> None:
        # An exit_spec that was not even buildable journals NO geometry blob;
        # the placed exit is still the brief ladder, so the gate must still
        # price it.
        broker, lines, _alerts = self._run_touch(
            tp1_target=62.60, reference_qty=1.0, watch_qty=1.0, stamp=False
        )
        self.assertEqual(broker.trailing_orders, [])
        self.assertEqual(len([ln for ln in lines if ln["kind"] == entry_trails.KIND_CANCELLED]), 1)


class TestEntryWatchPassTouchLatch(unittest.TestCase):
    """The 1 Hz touch-latch combine (entry_trailing_design §5): a sub-45s wick
    the coarse point-sample missed is folded in via the drained running low,
    gated so a re-arm open-check or a stale cross-session low can never arm into
    a gap, and drained ONCE per uic so every laddered tier sees the same low."""

    def _tick(
        self,
        deps: cl.LoopDeps,
        prices: dict[int, float | None],
        lows: dict[int, float | None],
        *,
        point: float | None,
        low: float | None,
    ) -> None:
        prices[307] = point
        lows[307] = low
        with mock.patch.dict("os.environ", _ALLOW, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())

    def test_sub_tick_wick_below_tier_touches_via_the_latch(self) -> None:
        path = _journal(self)
        _planned_journal(self)
        _seed_watch(path, crid="KO-2026-07-20-entry-t0", limit=10.0, next_tier_limit=None)
        _seed_healthy_plan()
        prices: dict[int, float | None] = {307: None}
        lows: dict[int, float | None] = {307: None}
        broker = _RecordingBroker()
        deps = _watch_deps(_FakeFeed(prices, lows), [], broker=broker)
        # Tick 1: a fresh point ABOVE the tier (no touch) — establishes the
        # latch's trust (a brand-new watcher discards the latch on its FIRST
        # fresh tick, when the accumulation window is unbounded).
        self._tick(deps, prices, lows, point=10.50, low=None)
        self.assertEqual(broker.trailing_orders, [])
        # Tick 2: the point-sample STILL sits above the tier (10.20 — the 45s
        # sample misses the touch) but the 1 Hz latch dipped to 9.90 → the
        # combine's min() registers the touch → exactly one native trail armed.
        self._tick(deps, prices, lows, point=10.20, low=9.90)
        self.assertEqual(len(broker.trailing_orders), 1)

    def test_latch_is_discarded_on_the_first_fresh_tick(self) -> None:
        # The very first fresh tick of a watch has an UNBOUNDED latch window (the
        # shared stream may have accumulated a running low for this uic long
        # before the watch opened, or across an overnight boundary). A latched
        # low below the tier must NOT touch/arm on tick 1 — the fresh
        # point-sample alone drives it (anti-arm-into-gap, entry_trailing §5).
        path = _journal(self)
        _planned_journal(self)
        _seed_watch(path, crid="KO-2026-07-20-entry-t0", limit=10.0, next_tier_limit=None)
        prices: dict[int, float | None] = {307: None}
        lows: dict[int, float | None] = {307: None}
        broker = _RecordingBroker()
        deps = _watch_deps(_FakeFeed(prices, lows), [], broker=broker)
        self._tick(deps, prices, lows, point=10.50, low=9.00)  # latch below tier, tick 1
        self.assertEqual(broker.trailing_orders, [])

    def test_one_drained_low_reaches_every_tier_on_the_same_uic(self) -> None:
        # A laddered pick: two tiers on the SAME uic, both active this tick. The
        # drained low is a POP, so it must be drained ONCE per uic and shared —
        # never consumed by whichever tier iterates first. A wick below BOTH tier
        # limits touches+arms BOTH in one tick; a per-tier pop would arm only one.
        path = _journal(self)
        _planned_journal(self)
        _seed_watch(path, crid="KO-2026-07-20-entry-t0", limit=10.0, next_tier_limit=None)
        _seed_healthy_plan()
        _seed_watch(path, crid="KO-2026-07-20-entry-t1", limit=9.90, next_tier_limit=None)
        prices: dict[int, float | None] = {307: None}
        lows: dict[int, float | None] = {307: None}
        broker = _RecordingBroker()
        deps = _watch_deps(_FakeFeed(prices, lows), [], broker=broker)
        self._tick(deps, prices, lows, point=10.50, low=None)  # establish trust
        self._tick(deps, prices, lows, point=10.50, low=9.80)  # wick below BOTH
        self.assertEqual(len(broker.trailing_orders), 2)

    def test_awaiting_fresh_low_ignores_the_latch_until_a_fresh_point_low(self) -> None:
        path = _journal(self)
        _planned_journal(self)
        crid = "KO-2026-07-20-entry-t0"
        # A re-armed tier: the watch_open carries the open-check marker plus a
        # carried (stale) trough of 9.0.
        entry_trails.append_entry_trail_line(
            {
                "kind": entry_trails.KIND_WATCH_OPEN,
                "crid": crid,
                "limit": 10.0,
                "qty": 100.0,
                "d_bps": 50,
                "window_end": "2099-01-01T21:00:00+00:00",
                "fx_rate": None,
                "uic": 307,
                "ticker": "KO",
                "exchange_mic": "XNYS",
                "next_tier_limit": None,
                "pick_key": "KO:2026-07-20",
                "entry_mode": "entry-trail-native-d50-testcfg",
                "disaster_stop": 8.0,
                "tier_index": 0,
                "awaiting_fresh_low": True,
            }
        )
        entry_trails.append_entry_trail_line(
            {"kind": entry_trails.KIND_TROUGH, "crid": crid, "trough": 9.0}
        )
        _seed_healthy_plan()
        prices: dict[int, float | None] = {307: None}
        lows: dict[int, float | None] = {307: None}
        broker = _RecordingBroker()
        deps = _watch_deps(_FakeFeed(prices, lows), [], broker=broker)
        # Tick 1: a fresh point ABOVE the tier establishes trust (no touch).
        self._tick(deps, prices, lows, point=10.50, low=None)
        watcher = deps.entry_watchers[crid].watcher
        self.assertTrue(watcher.awaiting_fresh_low)
        # Tick 2: the latch dips BELOW the carried trough (8.5 < 9.0), the point
        # stays above the tier. While awaiting_fresh_low the latch is IGNORED:
        # the open-check does not clear (G1 anti-arm-into-gap), no touch, no arm.
        self._tick(deps, prices, lows, point=10.50, low=8.5)
        self.assertTrue(watcher.awaiting_fresh_low)
        self.assertEqual(broker.trailing_orders, [])
        # Tick 3: a FRESH point-sample low below the tier AND the carried trough
        # clears the open-check → touch → the tier arms (the latch resumes).
        self._tick(deps, prices, lows, point=8.80, low=None)
        self.assertFalse(watcher.awaiting_fresh_low)
        self.assertEqual(len(broker.trailing_orders), 1)

    def test_point_veto_preserves_the_latched_low_for_the_next_fresh_tick(self) -> None:
        # THE 2026-08-18 LIVE incident (OLN): the 1 Hz latch recorded a REAL
        # touch (bid below the tier limit), but on that tick's point-sample the
        # change-driven stream had been quiet >3s so is_fresh vetoed it — and the
        # unconditional drain pop destroyed the latched evidence forever. The
        # doctrine "no watch decision without a fresh point-sample" STANDS (no
        # touch on the vetoed tick); the drained low must be handed BACK so a
        # later fresh tick can fold it in.
        path = _journal(self)
        _planned_journal(self)
        _seed_watch(path, crid="KO-2026-07-20-entry-t0", limit=10.0, next_tier_limit=None)
        _seed_healthy_plan()
        prices: dict[int, float | None] = {307: None}
        lows: dict[int, float | None] = {307: None}
        broker = _RecordingBroker()
        feed = _FakeFeed(prices, lows)
        deps = _watch_deps(feed, [], broker=broker)
        # Tick 1: a fresh point ABOVE the tier establishes the latch's trust.
        self._tick(deps, prices, lows, point=10.50, low=None)
        # Tick 2: the latch accrued 9.90 (below the 10.0 limit) but the
        # point-sample is veto-stale (None). NO decision this tick — and the
        # drained low is reseeded, once for the uic, instead of destroyed.
        self._tick(deps, prices, lows, point=None, low=9.90)
        self.assertEqual(broker.trailing_orders, [], "no decision without a fresh point")
        self.assertEqual([ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_TOUCHED], [])
        self.assertEqual(feed.reseeds, [(307, 9.90)], "the drained low was handed back, once")
        # Tick 3: the point recovers ABOVE the tier (10.20 — the coarse sample
        # still misses the dip). The PRESERVED low folds in via the combine →
        # touch + arm. Deliberately does NOT re-plant the low: it must come from
        # the tick-2 reseed alone.
        prices[307] = 10.20
        with mock.patch.dict("os.environ", _ALLOW, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())
        touched = [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_TOUCHED]
        self.assertEqual(len(touched), 1, "the preserved low registers the touch")
        self.assertEqual(len(broker.trailing_orders), 1)

    def test_untrusted_latch_discard_is_final_no_reseed(self) -> None:
        # The first fresh tick of a brand-new watcher has an UNBOUNDED latch
        # window, so its low is distrusted and the discard must stay FINAL —
        # reseeding it would let a pre-watch/pre-session wick survive until
        # trusted and fire into a gap (G1). The point-sample is FRESH here, so
        # the point-veto reseed must not fire either.
        path = _journal(self)
        _planned_journal(self)
        _seed_watch(path, crid="KO-2026-07-20-entry-t0", limit=10.0, next_tier_limit=None)
        prices: dict[int, float | None] = {307: None}
        lows: dict[int, float | None] = {307: None}
        broker = _RecordingBroker()
        feed = _FakeFeed(prices, lows)
        deps = _watch_deps(feed, [], broker=broker)
        self._tick(deps, prices, lows, point=10.50, low=9.00)  # tick 1: untrusted latch
        self.assertEqual(feed.reseeds, [], "an untrusted-latch discard never reseeds")
        self.assertEqual(broker.trailing_orders, [])
        # The discard really was final: tick 2 deliberately does NOT re-plant
        # the low (a `_tick(low=None)` would overwrite the fake's accumulator
        # and mask a wrong tick-1 reseed) — a resurrected 9.00 could only come
        # from that reseed, and the latch IS trusted on tick 2, so it would
        # fold in and touch+arm here. No touch, no arm.
        prices[307] = 10.50
        with mock.patch.dict("os.environ", _ALLOW, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())
        self.assertEqual(broker.trailing_orders, [])

    def test_awaiting_fresh_low_discard_is_final_no_reseed(self) -> None:
        # A re-armed tier's open-check discard (memo G1) distrusts the LOW
        # itself — it must stay final, never reseed (the point-sample is fresh,
        # so the point-veto reseed has no business firing either).
        path = _journal(self)
        _planned_journal(self)
        crid = "KO-2026-07-20-entry-t0"
        entry_trails.append_entry_trail_line(
            {
                "kind": entry_trails.KIND_WATCH_OPEN,
                "crid": crid,
                "limit": 10.0,
                "qty": 100.0,
                "d_bps": 50,
                "window_end": "2099-01-01T21:00:00+00:00",
                "fx_rate": None,
                "uic": 307,
                "ticker": "KO",
                "exchange_mic": "XNYS",
                "next_tier_limit": None,
                "pick_key": "KO:2026-07-20",
                "entry_mode": "entry-trail-native-d50-testcfg",
                "disaster_stop": 8.0,
                "tier_index": 0,
                "awaiting_fresh_low": True,
            }
        )
        entry_trails.append_entry_trail_line(
            {"kind": entry_trails.KIND_TROUGH, "crid": crid, "trough": 9.0}
        )
        prices: dict[int, float | None] = {307: None}
        lows: dict[int, float | None] = {307: None}
        broker = _RecordingBroker()
        feed = _FakeFeed(prices, lows)
        deps = _watch_deps(feed, [], broker=broker)
        self._tick(deps, prices, lows, point=10.50, low=None)  # establish trust
        self._tick(deps, prices, lows, point=10.50, low=8.5)  # open-check discard
        self.assertEqual(feed.reseeds, [], "an awaiting_fresh_low discard never reseeds")
        self.assertEqual(broker.trailing_orders, [])

    def test_reseed_preserved_low_dies_at_a_recovery_beyond_the_stale_gap(self) -> None:
        # The reseed's survival CEILING (G1 anti-gap): a preserved low may only
        # be ACTED on when the recovery tick lands within STALE_FIRE_GAP of the
        # watcher's last fresh tick. Every real halt pushes the recovery beyond
        # it (an LULD pause lasts >= 5 min == STALE_FIRE_GAP and starts after
        # the last fresh sample), so the combine discards the preserved low and
        # the point-sample alone drives the tick — and the discard is final,
        # because a fresh-point tick never reseeds (pinned by the finality
        # tests above). Clock-free at the combine seam (the pass itself reads
        # the wall clock).
        t0 = dt.datetime(2026, 8, 18, 16, 0, tzinfo=dt.UTC)
        config = entry_trail_watcher.TierWatchConfig(
            crid="KO-2026-07-20-entry-t0",
            tier_limit=10.0,
            d_bps=50,
            window_end=t0 + dt.timedelta(days=7),
            qty=100.0,
        )
        watcher = entry_trail_watcher.EntryTierWatcher(config, native_trail=True)
        watcher.process(entry_trail_watcher.TickInput(now=t0, price=10.50))  # last FRESH tick
        runtime = cl._EntryWatchRuntime(watcher=watcher)
        record = {"uic": 307}
        lows = {307: 9.90}  # the reseed-preserved wick, below the tier limit
        inside = t0 + entry_trail_watcher.STALE_FIRE_GAP
        self.assertEqual(
            cl._combine_with_session_low(10.20, lows, record, runtime, inside),
            9.90,
            "within the gap the preserved low folds in (the incident fix)",
        )
        beyond = t0 + entry_trail_watcher.STALE_FIRE_GAP + dt.timedelta(seconds=1)
        self.assertEqual(
            cl._combine_with_session_low(10.20, lows, record, runtime, beyond),
            10.20,
            "beyond the gap the preserved low is DISCARDED (halt-spanning recovery)",
        )

    def test_feed_without_session_low_capability_is_unchanged(self) -> None:
        from broker_contract.price_feed import SupportsSessionLow

        # The null/degraded feed is NOT a SupportsSessionLow (safe: yields no
        # latch); the latch-capable fake IS.
        self.assertNotIsInstance(cl._NullPriceFeed(), SupportsSessionLow)
        self.assertIsInstance(_FakeFeed({}), SupportsSessionLow)

        class _PointOnly:
            """A pre-latch PriceFeed shape — no session_low method."""

            def __init__(self, prices: dict[int, float | None]) -> None:
                self._p = prices

            def latest(self, uic: int) -> Any:
                v = self._p.get(uic)
                return None if v is None else type("PP", (), {"bid": v, "ask": v})()

        self.assertNotIsInstance(_PointOnly({}), SupportsSessionLow)

        path = _journal(self)
        _planned_journal(self)
        _seed_watch(path, crid="KO-2026-07-20-entry-t0", limit=10.0, next_tier_limit=None)
        _seed_healthy_plan()
        prices: dict[int, float | None] = {307: None}
        broker = _RecordingBroker()
        deps = _watch_deps(_PointOnly(prices), [], broker=broker)
        for pt in (10.50, 10.00):  # first establishes state, second touches @ limit
            prices[307] = pt
            with mock.patch.dict("os.environ", _ALLOW, clear=True):
                cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())
        self.assertEqual(len(broker.trailing_orders), 1)


# --------------------------------------------------------------------------
# Managed-exit state journaled at watch-open routing (2026-08-19 live incident)
# --------------------------------------------------------------------------


def _tranche(index: int, target: float, pct: float, *, r: float = 1.5) -> Any:
    from broker_contract.sizing import TpTranchePlan

    return TpTranchePlan(
        tranche_index=index, target_price=target, tranche_frac=pct, r_multiple=r, tag=f"tp{index}"
    )


def _plan_with_tranches(
    tiers: tuple[tuple[int, float, int], ...], tranches: tuple[Any, ...]
) -> SetupPlan:
    base = _plan(*tiers)
    return SetupPlan(
        suggested_size_pct=base.suggested_size_pct,
        scale_factor=base.scale_factor,
        final_size_pct=base.final_size_pct,
        total_notional=base.total_notional,
        paper_equity=base.paper_equity,
        disaster_stop=base.disaster_stop,
        order_ttl_days=base.order_ttl_days,
        entry_tiers=base.entry_tiers,
        tp_tranches=tranches,
    )


def _exit_spec(stop: float | None, tp: float | None) -> Any:
    levels = type("Levels", (), {"stop": stop, "tp": tp})()
    return type("ExitSpec", (), {"initial_levels": levels, "reaction_plan": ()})()


def _blend_spec() -> Any:
    tier = type("SpecTier", (), {"limit_price": 10.0, "alloc_pct": 100.0})()
    return type("Spec", (), {"entry_tiers": (tier,)})()


# A real registry policy, not a one-attribute duck type. The stamp reads
# ``exit_policy.name`` (#1138) and the watch-open path degrades to "pick stays
# armed" on ANY exception, so a stub missing a protocol attribute turns a
# behaviour test into a silent False. `atr_bracket_1p5` is the non-trailing
# bracket, which is what this suite exercised all along.
_GEOMETRY_POLICY = resolve_exit_policy("atr_bracket_1p5")


def _route_watch(
    test: unittest.TestCase,
    plan: SetupPlan,
    *,
    intent: Any = None,
    exit_policy: Any = None,
) -> tuple[bool, list[dict[str, Any]], Path, Path]:
    """Drive ``_route_pick_to_entry_watch`` hermetically (temp journals, stubbed
    submission log) and return (verdict, tranche_plan lines, journal paths)."""
    trails_path = _journal(test)
    stops_path = _planned_journal(test)
    pkg = "alphalens_pipeline.brokers"
    for target, fn in (
        (f"{pkg}.submission_log.build_submission_record", lambda **kw: dict(kw)),
        (f"{pkg}.submission_log.append_submission_record", lambda _r: None),
    ):
        test.enterContext(mock.patch(target, fn))
    with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
        ok = cl._route_pick_to_entry_watch(
            object(),
            intent if intent is not None else _pick(),
            "KO",
            _instr(),
            _acct(),
            plan,
            None,
            d_bps=50,
            exit_policy=exit_policy,
        )
    tranche_lines = [ln for ln in _lines(stops_path) if ln["kind"] == "tranche_plan"]
    return ok, tranche_lines, trails_path, stops_path


class TestWatchRoutingJournalsTranchePlan(unittest.TestCase):
    """Task A1 (2026-08-19 live incident, OLN): the entry-trail routing must
    journal the SAME per-uic ``tranche_plan`` line the bracket path writes at
    placement — without it the live-exit engine skips the filled position every
    tick ("no tranche_plan on record") and the position gets NO TP management."""

    def _route(
        self,
        plan: SetupPlan,
        *,
        intent: Any = None,
        exit_policy: Any = None,
    ) -> tuple[bool, list[dict[str, Any]], Path, Path]:
        return _route_watch(self, plan, intent=intent, exit_policy=exit_policy)

    def test_static_policy_journals_the_plan_ladder_with_watch_reference_qty(self) -> None:
        # A zero-qty tier opens NO watch — reference_qty counts only the tiers
        # that actually watch (the fill base the live-exit engine scales from).
        plan = _plan_with_tranches(
            ((0, 10.0, 100), (1, 9.0, 0)), (_tranche(0, 14.0, 0.6), _tranche(1, 16.0, 0.4))
        )
        ok, tranche_lines, _trails, _stops = self._route(plan)
        self.assertTrue(ok)
        self.assertEqual(len(tranche_lines), 1)
        line = tranche_lines[0]
        self.assertEqual(line["uic"], 307)
        self.assertEqual(line["stop_price"], 8.0)  # plan.disaster_stop
        self.assertEqual(line["reference_qty"], 100.0)  # positive-qty WATCH tiers only
        self.assertEqual([t["target_price"] for t in line["tp_tranches"]], [14.0, 16.0])

    def test_geometry_policy_journals_the_single_geometry_tranche(self) -> None:
        plan = _plan_with_tranches(((0, 10.0, 100),), (_tranche(0, 14.0, 1.0),))
        intent = _pick()
        intent.exit = _exit_spec(stop=9.1, tp=13.5)
        intent.spec = _blend_spec()
        ok, tranche_lines, _trails, _stops = self._route(
            plan, intent=intent, exit_policy=_GEOMETRY_POLICY
        )
        self.assertTrue(ok)
        self.assertEqual(len(tranche_lines), 1)
        line = tranche_lines[0]
        self.assertEqual(line["stop_price"], 9.1)  # geometry stop, NOT plan.disaster_stop
        self.assertEqual(
            line["tp_tranches"],
            [
                {
                    "tranche_index": 0,
                    "target_price": 13.5,
                    "tranche_frac": 1.0,
                    "r_multiple": 0.0,
                    "tag": "geometry",
                }
            ],
        )

    def test_unusable_geometry_levels_skip_the_ladder_and_warn_but_still_watch(self) -> None:
        plan = _plan_with_tranches(((0, 10.0, 100),), (_tranche(0, 14.0, 1.0),))
        intent = _pick()
        intent.exit = _exit_spec(stop=None, tp=13.5)
        intent.spec = _blend_spec()
        with self.assertLogs(cl.logger, level="WARNING") as captured:
            ok, tranche_lines, trails_path, _stops = self._route(
                plan, intent=intent, exit_policy=_GEOMETRY_POLICY
            )
        self.assertTrue(ok)  # the watch itself still opens (stop-only, like brackets)
        self.assertEqual(tranche_lines, [])
        self.assertTrue(any("geometry levels unusable" in msg for msg in captured.output))
        opens = [ln for ln in _lines(trails_path) if ln["kind"] == entry_trails.KIND_WATCH_OPEN]
        self.assertEqual(len(opens), 1)

    def test_tranche_plan_line_carries_the_pick_identity(self) -> None:
        # 2026-08-19 adjudication finding 4: the watch path stamps pick_key so
        # the fired-tranche fold treats a re-drive's re-append as the SAME
        # trade (no fired-set reset).
        plan = _plan_with_tranches(((0, 10.0, 100),), (_tranche(0, 14.0, 1.0),))
        ok, tranche_lines, _trails, _stops = self._route(plan)
        self.assertTrue(ok)
        self.assertEqual(tranche_lines[0]["pick_key"], "KO:2026-07-20")

    def test_empty_static_ladder_journals_no_tranche_plan(self) -> None:
        ok, tranche_lines, _trails, _stops = self._route(_plan((0, 10.0, 100)))
        self.assertTrue(ok)
        self.assertEqual(tranche_lines, [])

    def test_tranche_plan_is_appended_before_the_watch_open_lines(self) -> None:
        # Crash ordering: a crash between the two journals must never leave a
        # watching tier without its ladder — so the ladder goes to disk FIRST.
        events: list[str] = []
        self.enterContext(
            mock.patch.object(
                cl, "_append_standalone_stop_journal", lambda line: events.append(line["kind"])
            )
        )
        self.enterContext(
            mock.patch.object(
                entry_trails, "append_entry_trail_line", lambda line: events.append(line["kind"])
            )
        )
        plan = _plan_with_tranches(((0, 10.0, 100),), (_tranche(0, 14.0, 1.0),))
        ok, _tranche_lines, _trails, _stops = self._route(plan)
        self.assertTrue(ok)
        self.assertIn("tranche_plan", events)
        self.assertIn(entry_trails.KIND_WATCH_OPEN, events)
        self.assertLess(events.index("tranche_plan"), events.index(entry_trails.KIND_WATCH_OPEN))


class TestEntryWatchCapacityEnvRail(unittest.TestCase):
    """Task B (2026-08-19 live incident, ETSY): the pick-denominated watch cap
    was a hardcoded constant of 1 — with MAX_OPEN raised to 2 a second armed
    pick was silently capacity-deferred forever at DEBUG. The cap becomes a
    call-time env rail (ALPHALENS_BROKER_ENTRY_WATCH_MAX_PICKS, default 1,
    valid [1, 25]) and the FIRST deferral of a pick logs at INFO.

    The ceiling was 10 until #1189 raised it for the SIM soak; LIVE is held to
    [1, 2] by its own boot-assert pin, not by this shared bound."""

    def setUp(self) -> None:
        # Reset the process-lifetime observability state so tests are hermetic.
        self.enterContext(mock.patch.object(cl, "_entry_watch_max_picks_warned", False))
        self.enterContext(mock.patch.object(cl, "_entry_watch_capacity_deferred", set()))

    def test_unset_env_defaults_to_one(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(cl._entry_watch_max_picks(), 1)

    def test_valid_values_are_honoured(self) -> None:
        for raw, expected in (("1", 1), ("2", 2), ("4", 4), ("5", 5), ("10", 10), ("25", 25)):
            with mock.patch.dict("os.environ", {cl._ENTRY_WATCH_MAX_PICKS_ENV: raw}, clear=True):
                self.assertEqual(cl._entry_watch_max_picks(), expected)

    def test_invalid_value_falls_back_to_one_and_warns_exactly_once(self) -> None:
        with (
            mock.patch.dict("os.environ", {cl._ENTRY_WATCH_MAX_PICKS_ENV: "banana"}, clear=True),
            self.assertLogs(cl.logger, level="WARNING") as captured,
        ):
            self.assertEqual(cl._entry_watch_max_picks(), 1)
            self.assertEqual(cl._entry_watch_max_picks(), 1)  # second read: no new warning
        warnings = [m for m in captured.output if cl._ENTRY_WATCH_MAX_PICKS_ENV in m]
        self.assertEqual(len(warnings), 1)

    def test_out_of_range_values_fall_back_to_one(self) -> None:
        for raw in ("0", "26", "-1", ""):
            with (
                self.subTest(raw=raw),
                mock.patch.object(cl, "_entry_watch_max_picks_warned", False),
                mock.patch.dict("os.environ", {cl._ENTRY_WATCH_MAX_PICKS_ENV: raw}, clear=True),
                self.assertLogs(cl.logger, level="WARNING"),
            ):
                self.assertEqual(cl._entry_watch_max_picks(), 1)

    def _seed_other_watch(self) -> Path:
        path = _journal(self)
        entry_trails.append_entry_trail_line(
            {
                "kind": entry_trails.KIND_WATCH_OPEN,
                "crid": "OTHER-2026-07-19-entry-t0",
                "limit": 5.0,
                "qty": 10.0,
                "pick_key": "OTHER:2026-07-19",
            }
        )
        return path

    def test_cap_two_lets_a_second_pick_open_its_watch(self) -> None:
        path = self._seed_other_watch()
        broker = _RecordingBroker()
        placer, _submissions = _placer(self, broker, _plan((0, 10.0, 100)))
        env = {_ENV: "50", cl._ENTRY_WATCH_MAX_PICKS_ENV: "2"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertTrue(placer(_pick()))  # NOT deferred at cap=2
        opens_for_ko = [
            ln
            for ln in _lines(path)
            if ln["kind"] == entry_trails.KIND_WATCH_OPEN and ln.get("ticker") == "KO"
        ]
        self.assertEqual(len(opens_for_ko), 1)

    def test_first_capacity_deferral_logs_info_then_debug(self) -> None:
        self._seed_other_watch()
        broker = _RecordingBroker()
        placer, _submissions = _placer(self, broker, _plan((0, 10.0, 100)))
        with (
            mock.patch.dict("os.environ", {_ENV: "50"}, clear=True),
            self.assertLogs(cl.logger, level="DEBUG") as captured,
        ):
            self.assertFalse(placer(_pick()))  # first deferral -> INFO
            self.assertFalse(placer(_pick()))  # every later tick -> DEBUG
        records = [r for r in captured.records if "capacity reached" in r.getMessage()]
        self.assertEqual([r.levelname for r in records], ["INFO", "DEBUG"])
        self.assertIn("cap=1", records[0].getMessage())
        self.assertIn("KO", records[0].getMessage())
        self.assertIn("stays armed", records[0].getMessage())


class TestWatchGeometryStampThroughToPlannedLine(unittest.TestCase):
    """Task A2 (2026-08-19 live incident, OLN): the geometry blob must ride the
    watch_open line so the fire-arm ``planned`` disaster line carries it —
    without the stamp ``_reanchor_facts_from_governing`` has no (k_atr, atr)
    facts and the position NEVER trails."""

    def test_watch_open_lines_carry_the_geometry_stamp(self) -> None:
        plan = _plan((0, 10.0, 100), (1, 9.0, 100))
        intent = _pick()
        intent.exit = _exit_spec(stop=9.1, tp=13.5)
        intent.spec = _blend_spec()
        expected = cl._geometry_shadow_stamp(
            intent.exit, intent.spec, use_geometry=True, exit_policy=_GEOMETRY_POLICY
        )
        ok, _tranche_lines, trails_path, _stops = _route_watch(
            self, plan, intent=intent, exit_policy=_GEOMETRY_POLICY
        )
        self.assertTrue(ok)
        opens = [ln for ln in _lines(trails_path) if ln["kind"] == entry_trails.KIND_WATCH_OPEN]
        self.assertEqual(len(opens), 2)
        for line in opens:
            self.assertEqual(line["geometry"], expected)

    def test_watch_open_without_exit_spec_omits_the_geometry_key(self) -> None:
        # _pick() carries exit=None -> the stamp is None -> the key is absent
        # (old-line byte-identity: readers treat a missing key as no geometry).
        ok, _tranche_lines, trails_path, _stops = _route_watch(self, _plan((0, 10.0, 100)))
        self.assertTrue(ok)
        opens = [ln for ln in _lines(trails_path) if ln["kind"] == entry_trails.KIND_WATCH_OPEN]
        self.assertEqual(len(opens), 1)
        self.assertNotIn("geometry", opens[0])

    def test_fire_arm_planned_line_carries_the_record_geometry_stamp(self) -> None:
        stops_path = _planned_journal(self)
        stamp = {
            "policy_name": "atr_bracket_1p5",
            "policy_version": 1,
            "planned_blend": 10.0,
            "geometry_stop": 9.1,
            "geometry_tp": 13.5,
            "k_atr": 1.5,
            "atr": 0.6,
            "ceiling_price": None,
            "applied": True,
        }
        record = {"disaster_stop": 8.0, "tier_index": 0, "geometry": stamp}
        cl._journal_entry_planned_disaster(record, 307, "KO-2026-07-20-entry-t0-fire")
        planned = [ln for ln in _lines(stops_path) if ln["kind"] == "planned"]
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["geometry"], stamp)

    def test_fire_arm_planned_line_without_geometry_is_byte_identical(self) -> None:
        # An OLD watch_open line (journaled before the stamp existed) must keep
        # producing EXACTLY today's planned line — no new key, no reordering.
        stops_path = _planned_journal(self)
        record = {"disaster_stop": 8.0, "tier_index": 0}
        cl._journal_entry_planned_disaster(record, 307, "KO-2026-07-20-entry-t0-fire")
        planned = [ln for ln in _lines(stops_path) if ln["kind"] == "planned"]
        self.assertEqual(
            planned,
            [
                {
                    "kind": "planned",
                    "client_request_id": "KO-2026-07-20-entry-t0-fire",
                    "uic": 307,
                    "side": "SELL",
                    "stop_price": 8.0,
                    "take_profit": None,
                    "tier_index": 0,
                    "gen": 0,
                }
            ],
        )

    def test_stamp_survives_the_fold_from_watch_open_to_the_armed_planned_line(self) -> None:
        # End-to-end: a seeded watch_open WITH the stamp -> touch -> native arm
        # -> the fire-arm planned line carries the stamp read off the FOLDED
        # record (pins the verbatim watch_open fold, not just the writer).
        path = _journal(self)
        stops_path = _planned_journal(self)
        stamp = {"policy_name": "atr_bracket_1p5", "k_atr": 1.5, "atr": 0.6, "applied": True}
        entry_trails.append_entry_trail_line(
            {
                "kind": entry_trails.KIND_WATCH_OPEN,
                "crid": "KO-2026-07-20-entry-t0",
                "limit": 10.0,
                "qty": 100.0,
                "d_bps": 50,
                "window_end": "2099-01-01T21:00:00+00:00",
                "fx_rate": None,
                "uic": 307,
                "ticker": "KO",
                "exchange_mic": "XNYS",
                "next_tier_limit": None,
                "pick_key": "KO:2026-07-20",
                "entry_mode": "entry-trail-native-d50-testcfg",
                "disaster_stop": 8.0,
                "tier_index": 0,
                "geometry": stamp,
            }
        )
        _seed_healthy_plan()
        prices: dict[int, float | None] = {307: 10.0}  # touch @ limit -> ARM
        broker = _RecordingBroker()
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)
        with mock.patch.dict("os.environ", _ALLOW, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())
        self.assertEqual(len(broker.trailing_orders), 1)
        planned = [ln for ln in _lines(stops_path) if ln["kind"] == "planned"]
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["geometry"], stamp)


class TestPointSampleVetoNotRaise(unittest.TestCase):
    """_point_sample_bids is the shared once-per-uic sampling boundary: a
    structurally invalid PricePoint (non-numeric bid despite the protocol)
    must veto that uic, never abort the entry-watch pass and starve the
    protection pass that runs after it."""

    def test_non_numeric_bid_vetoes_the_uic_instead_of_raising(self):
        class _WeirdPoint:
            bid = "garbage"

        class _WeirdFeed:
            def latest(self, uic: int) -> object:
                return _WeirdPoint()

        points = cl._point_sample_bids(_WeirdFeed(), {307: ("KO", "XNYS")})
        self.assertEqual(points, {307: None})


class TestEntryWatchPassKillGate(unittest.TestCase):
    def test_kill_writes_nothing_and_alerts_nothing(self) -> None:
        path = _journal(self)
        _seed_watch(path, crid="KO-2026-07-20-entry-t0", limit=10.0, next_tier_limit=None)
        before = _lines(path)
        prices: dict[int, float | None] = {307: 10.0}  # would touch if processed
        alerts: list[tuple[str, str]] = []
        deps = _watch_deps(_FakeFeed(prices), alerts)
        with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
            cl._run_entry_watch_pass(deps, kill=True, report=cl.TickReport())
        self.assertEqual(_lines(path), before)  # no journal writes under KILL
        self.assertEqual(alerts, [])  # no alerts under KILL
        self.assertEqual(deps.entry_watchers, {})  # no watcher constructed

    def test_flag_off_is_a_noop(self) -> None:
        path = _journal(self)
        _seed_watch(path, crid="KO-2026-07-20-entry-t0", limit=10.0, next_tier_limit=None)
        before = _lines(path)
        prices: dict[int, float | None] = {307: 10.0}
        deps = _watch_deps(_FakeFeed(prices), [])
        with mock.patch.dict("os.environ", {}, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())
        self.assertEqual(_lines(path), before)

    def test_kill_cancels_working_entry_family_orders(self) -> None:
        # Memo §3 G2: under KILL the pass cancels every working -entry- family
        # order (cancelling is risk-reducing, ungated by ALLOW_ORDERS) — but
        # leaves the protective SELL disaster stop in place.
        prices: dict[int, float | None] = {307: 10.0}
        broker = _RecordingBroker()
        broker.open_orders = [
            type(
                "OS",
                (),
                {
                    "order_id": "TR-1",
                    "external_reference": "KO-2026-07-20-entry-t0-fire",
                    "side": "BUY",
                },
            )(),
            type(
                "OS",
                (),
                {"order_id": "SELL-STOP", "external_reference": "rid-0-stop", "side": "SELL"},
            )(),
        ]
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)
        with mock.patch.dict("os.environ", _ALLOW, clear=True):
            cl._run_entry_watch_pass(deps, kill=True, report=cl.TickReport())
        self.assertEqual(broker.cancels, ["TR-1"], "only the -entry- BUY order is cancelled")


class TestEntryWatchFeedScope(unittest.TestCase):
    """The entry-watch slice of the shared price-stream subscription
    (2026-08-18 churn-fix follow-up). The active path must claim the
    "entry-watch" scope — collapsing it into the exits scope would
    reintroduce the alternating {} <-> {watch uic} subscription churn the
    scope split killed — and every quiet early-return (all watches terminal,
    KILL, feature off) must hand the scope an EMPTY set, or the last watch's
    uic stays in the wire-level union for the daemon's lifetime with zero
    price consumers."""

    def _capturing_deps(
        self,
        prices: dict[int, float | None],
        calls: list[tuple[dict[int, tuple[str, str]], str]],
        broker: Any = None,
    ) -> cl.LoopDeps:
        feed = _FakeFeed(prices)

        def factory(u2i: Any, *, scope: str) -> Any:
            calls.append((dict(u2i), scope))
            return feed

        return _watch_deps(feed, [], broker=broker, factory=factory)

    def test_an_active_watch_claims_the_entry_watch_scope(self) -> None:
        path = _journal(self)
        _seed_watch(path, crid="KO-2026-07-20-entry-t0", limit=10.0, next_tier_limit=None)
        calls: list[tuple[dict[int, tuple[str, str]], str]] = []
        deps = self._capturing_deps({307: 10.50}, calls)  # above the limit: no touch
        with mock.patch.dict("os.environ", _ALLOW, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())
        self.assertEqual(calls, [({307: ("KO", "XNYS")}, "entry-watch")])

    def test_all_watches_terminal_releases_the_scope(self) -> None:
        path = _journal(self)
        _seed_watch(path, crid="KO-2026-07-20-entry-t0", limit=10.0, next_tier_limit=None)
        entry_trails.append_entry_trail_line(
            {"kind": entry_trails.KIND_EXPIRED, "crid": "KO-2026-07-20-entry-t0"}
        )
        calls: list[tuple[dict[int, tuple[str, str]], str]] = []
        deps = self._capturing_deps({}, calls)
        with mock.patch.dict("os.environ", _ALLOW, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())
        self.assertEqual(calls, [({}, "entry-watch")])

    def test_kill_releases_the_scope(self) -> None:
        _journal(self)
        calls: list[tuple[dict[int, tuple[str, str]], str]] = []
        deps = self._capturing_deps({}, calls, broker=_RecordingBroker())
        with mock.patch.dict("os.environ", _ALLOW, clear=True):
            cl._run_entry_watch_pass(deps, kill=True, report=cl.TickReport())
        self.assertEqual(calls, [({}, "entry-watch")])

    def test_feature_off_releases_the_scope(self) -> None:
        _journal(self)
        calls: list[tuple[dict[int, tuple[str, str]], str]] = []
        deps = self._capturing_deps({}, calls)
        with mock.patch.dict("os.environ", {}, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())
        self.assertEqual(calls, [({}, "entry-watch")])


# --------------------------------------------------------------------------
# Open watches count against the MAX_OPEN admission (2026-08-19 adjudication)
# --------------------------------------------------------------------------


def _live_long(uic: int, qty: float = 8.0) -> Any:
    instr = type("I", (), {"broker_instrument_id": str(uic), "currency": "USD"})()
    return type(
        "Pos",
        (),
        {
            "instrument": instr,
            "quantity": qty,
            "avg_price": 10.0,
            "market_value": qty * 10.0,
            "unrealized_pnl": 0.0,
            "position_id": f"pos-{uic}",
        },
    )()


class _BrokerWithPositions(_RecordingBroker):
    """A recording broker whose ``get_positions`` returns injected positions."""

    def __init__(self, positions: list[Any]) -> None:
        super().__init__()
        self._positions = positions

    def get_positions(self) -> list:
        return self._positions


def _seed_other_watch_line(pick_key: str, *, crid: str, uic: int | None = None) -> None:
    line: dict[str, Any] = {
        "kind": entry_trails.KIND_WATCH_OPEN,
        "crid": crid,
        "limit": 5.0,
        "qty": 10.0,
        "pick_key": pick_key,
    }
    if uic is not None:
        line["uic"] = uic
    entry_trails.append_entry_trail_line(line)


class TestOpenWatchesCountAgainstMaxOpen(unittest.TestCase):
    """Adjudication finding 1 (2026-08-19): an open entry watch — or its armed
    unfilled native trail — is a committed risk unit the MAX_OPEN rail cannot
    see: the note-only watch submission record carries no brackets and no
    position exists until the trail fires, so ``safety.check``'s sum (journal
    brackets + live positions) misses it entirely and a watch capacity of N
    could over-commit up to N extra concurrent positions. ``_place_pick`` must
    count the DISTINCT watch-holding picks into the MAX_OPEN input."""

    _CHECK_TARGET = "alphalens_pipeline.brokers.automanager.safety.check"

    def _recording_check(self) -> list[Any]:
        seen: list[Any] = []

        def check(_pick: Any, journal_view: Any, broker_view: Any, _state: Any) -> Any:
            seen.append((journal_view, broker_view))
            # A transient Refuse stops _place_pick right after the check: these
            # tests pin the INPUTS to the rail, not the downstream placement.
            return _safety.Refuse("recorded — stop here")

        self.enterContext(mock.patch(self._CHECK_TARGET, check))
        return seen

    def test_another_picks_open_watch_raises_the_max_open_input(self) -> None:
        _journal(self)
        _seed_other_watch_line("OTHER:2026-07-19", crid="OTHER-2026-07-19-entry-t0")
        broker = _RecordingBroker()
        placer, _submissions = _placer(self, broker, _plan((0, 10.0, 100)))
        seen = self._recording_check()
        env = {_ENV: "50", cl._ENTRY_WATCH_MAX_PICKS_ENV: "2"}
        with mock.patch.dict("os.environ", env, clear=True):
            placer(_pick())
        journal_view, _broker_view = seen[0]
        self.assertEqual(journal_view.open_bracket_count, 1)

    def test_own_watch_is_excluded_from_the_max_open_input(self) -> None:
        # Crash-recovery re-drive: the pick's OWN watch must not self-block the
        # retirement (mirrors the intercept's already_watching exemption).
        _journal(self)
        _seed_other_watch_line("KO:2026-07-20", crid="KO-2026-07-20-entry-t0")
        broker = _RecordingBroker()
        placer, _submissions = _placer(self, broker, _plan((0, 10.0, 100)))
        seen = self._recording_check()
        with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
            placer(_pick())
        journal_view, _broker_view = seen[0]
        self.assertEqual(journal_view.open_bracket_count, 0)

    def test_watch_on_a_uic_with_a_live_position_is_not_double_counted(self) -> None:
        # A pick whose tier already FIRED shows up as a live position while a
        # deeper tier still watches — one risk unit, not two: the position side
        # is already in BrokerView.open_position_count.
        _journal(self)
        _seed_other_watch_line("OTHER:2026-07-19", crid="OTHER-2026-07-19-entry-t1", uic=42)
        broker = _BrokerWithPositions([_live_long(42)])
        placer, _submissions = _placer(self, broker, _plan((0, 10.0, 100)))
        seen = self._recording_check()
        with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
            placer(_pick())
        journal_view, broker_view = seen[0]
        self.assertEqual(journal_view.open_bracket_count, 0)
        self.assertEqual(broker_view.open_position_count, 1)

    def test_terminal_watches_do_not_count(self) -> None:
        _journal(self)
        _seed_other_watch_line("OTHER:2026-07-19", crid="OTHER-2026-07-19-entry-t0")
        entry_trails.append_entry_trail_line(
            {"kind": entry_trails.KIND_EXPIRED, "crid": "OTHER-2026-07-19-entry-t0"}
        )
        broker = _RecordingBroker()
        placer, _submissions = _placer(self, broker, _plan((0, 10.0, 100)))
        seen = self._recording_check()
        with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
            placer(_pick())
        journal_view, _broker_view = seen[0]
        self.assertEqual(journal_view.open_bracket_count, 0)

    def test_max_open_reached_by_watches_refuses_the_pick_terminal(self) -> None:
        # End-to-end with the REAL safety.check: two foreign open watches +
        # MAX_OPEN=2 -> the third pick is refused terminal BEFORE any watch
        # opens, even though the watch-capacity env rail would still admit it.
        path = _journal(self)
        _seed_other_watch_line("OTHER1:2026-07-19", crid="OTHER1-2026-07-19-entry-t0")
        _seed_other_watch_line("OTHER2:2026-07-19", crid="OTHER2-2026-07-19-entry-t0")
        broker = _RecordingBroker()
        placer, submissions = _placer(self, broker, _plan((0, 10.0, 100)))
        self.enterContext(mock.patch(self._CHECK_TARGET, _REAL_SAFETY_CHECK))
        refused: list[tuple[Any, ...]] = []
        self.enterContext(
            mock.patch(
                "alphalens_pipeline.brokers.automanager.picks.mark_refused",
                lambda *a, **k: refused.append(a),
            )
        )
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        no_kill = Path(tmp.name) / "KILL"
        for target in ("kill_file_path", "global_kill_file_path"):
            self.enterContext(
                mock.patch(
                    f"alphalens_pipeline.brokers.automanager.state_paths.{target}",
                    lambda: no_kill,
                )
            )
        env = {
            _ENV: "50",
            "ALPHALENS_BROKER_ALLOW_ORDERS": "1",
            "ALPHALENS_BROKER_MAX_OPEN": "2",
            cl._ENTRY_WATCH_MAX_PICKS_ENV: "4",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertFalse(placer(_pick()))
        self.assertEqual(len(refused), 1)
        self.assertIn("MAX_OPEN", refused[0][2])
        self.assertEqual(submissions, [])
        opens_for_ko = [
            ln
            for ln in _lines(path)
            if ln["kind"] == entry_trails.KIND_WATCH_OPEN and ln.get("ticker") == "KO"
        ]
        self.assertEqual(opens_for_ko, [])


class TestEodNettingRowsAreNetRiskUnits(unittest.TestCase):
    """LIVE Saxo accounts run End-Of-Day netting
    (``ClosedPositionNotAccessibleInEndOfDayNettingMode``): positions net only
    at EOD, so an intraday round-trip leaves TWO ledger rows (+q and -q) that
    net to zero until the nightly netting. MAX_OPEN counts RISK UNITS, not
    ledger rows — live incident 2026-08-19: a NET-FLAT book showed 2 rows and
    every drain tick terminally refused a valid pick on the MAX_OPEN rail.
    A net-flat uic must occupy no slot AND must not suppress an open watch
    from counting (the watch exclusion exists only because the position side
    is already counted — a net-flat uic is not)."""

    _CHECK_TARGET = "alphalens_pipeline.brokers.automanager.safety.check"

    def _recording_check(self) -> list[Any]:
        seen: list[Any] = []

        def check(_pick: Any, journal_view: Any, broker_view: Any, _state: Any) -> Any:
            seen.append((journal_view, broker_view))
            # A transient Refuse stops _place_pick right after the check: these
            # tests pin the INPUTS to the rail, not the downstream placement.
            return _safety.Refuse("recorded — stop here")

        self.enterContext(mock.patch(self._CHECK_TARGET, check))
        return seen

    def test_net_flat_round_trip_rows_occupy_no_slot(self) -> None:
        _journal(self)
        broker = _BrokerWithPositions([_live_long(42, qty=8.0), _live_long(42, qty=-8.0)])
        placer, _submissions = _placer(self, broker, _plan((0, 10.0, 100)))
        seen = self._recording_check()
        with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
            placer(_pick())
        _journal_view, broker_view = seen[0]
        self.assertEqual(broker_view.open_position_count, 0)

    def test_partially_closed_long_occupies_one_slot(self) -> None:
        _journal(self)
        broker = _BrokerWithPositions([_live_long(42, qty=8.0), _live_long(42, qty=-3.0)])
        placer, _submissions = _placer(self, broker, _plan((0, 10.0, 100)))
        seen = self._recording_check()
        with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
            placer(_pick())
        _journal_view, broker_view = seen[0]
        self.assertEqual(broker_view.open_position_count, 1)

    def test_two_net_open_uics_occupy_two_slots(self) -> None:
        _journal(self)
        broker = _BrokerWithPositions([_live_long(42, qty=8.0), _live_long(43, qty=5.0)])
        placer, _submissions = _placer(self, broker, _plan((0, 10.0, 100)))
        seen = self._recording_check()
        with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
            placer(_pick())
        _journal_view, broker_view = seen[0]
        self.assertEqual(broker_view.open_position_count, 2)

    def test_net_flat_uic_does_not_suppress_an_open_watch(self) -> None:
        # The watch-count exclusion (finding 1) exists because a FIRED tier's
        # position is already inside open_position_count. A net-flat uic is
        # NOT counted there, so its watch must keep occupying a slot.
        _journal(self)
        _seed_other_watch_line("OTHER:2026-07-19", crid="OTHER-2026-07-19-entry-t0", uic=42)
        broker = _BrokerWithPositions([_live_long(42, qty=8.0), _live_long(42, qty=-8.0)])
        placer, _submissions = _placer(self, broker, _plan((0, 10.0, 100)))
        seen = self._recording_check()
        env = {_ENV: "50", cl._ENTRY_WATCH_MAX_PICKS_ENV: "2"}
        with mock.patch.dict("os.environ", env, clear=True):
            placer(_pick())
        journal_view, broker_view = seen[0]
        self.assertEqual(broker_view.open_position_count, 0)
        self.assertEqual(journal_view.open_bracket_count, 1)


# --------------------------------------------------------------------------
# Routing defers while a live long already holds the uic (2026-08-19 adj. F2)
# --------------------------------------------------------------------------


class TestRoutingDefersOnLiveSameUicLong(unittest.TestCase):
    """Adjudication finding 2 (2026-08-19): routing a re-picked ticker into a
    watch while an earlier live long still holds the SAME uic would journal a
    fresh ``tranche_plan`` at watch-open time — replacing the live position's
    ladder and resetting its fired-tranche set with NO order ever placed (the
    live-exit engine could then re-sell the runner at the new pick's targets).
    The intercept defers such a pick (stays armed until the uic is flat); the
    ``already_watching`` re-drive is exempt — the pick's OWN fill must not
    deadlock the retirement record."""

    def setUp(self) -> None:
        self.enterContext(mock.patch.object(cl, "_entry_watch_live_uic_deferred", set()))

    def test_live_long_on_the_pick_uic_defers_and_journals_nothing(self) -> None:
        path = _journal(self)
        broker = _BrokerWithPositions([_live_long(307)])  # _instr() uic
        placer, submissions = _placer(self, broker, _plan((0, 10.0, 100)))
        with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
            self.assertFalse(placer(_pick()))  # deferred, stays armed
        self.assertEqual(_lines(path), [])  # no watch_open lines
        self.assertEqual(submissions, [])  # never retired
        self.assertEqual(broker.brackets, [])  # and no fall-through bracket

    def test_flat_or_short_rows_on_the_uic_do_not_defer(self) -> None:
        path = _journal(self)
        broker = _BrokerWithPositions([_live_long(307, qty=0.0), _live_long(307, qty=-5.0)])
        placer, _submissions = _placer(self, broker, _plan((0, 10.0, 100)))
        with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
            self.assertTrue(placer(_pick()))
        opens = [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_WATCH_OPEN]
        self.assertEqual(len(opens), 1)

    def test_a_live_long_on_a_different_uic_does_not_defer(self) -> None:
        path = _journal(self)
        broker = _BrokerWithPositions([_live_long(999)])
        placer, _submissions = _placer(self, broker, _plan((0, 10.0, 100)))
        with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
            self.assertTrue(placer(_pick()))
        opens = [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_WATCH_OPEN]
        self.assertEqual(len(opens), 1)

    def test_own_redrive_with_its_own_fill_is_not_deferred(self) -> None:
        # Crash-recovery: KO's t0 fired (live long on 307) while t1's watch is
        # still open and the retiring submission record was never written. The
        # re-drive must complete (re-open idempotently + retire), NOT deadlock
        # on its own fill.
        _journal(self)
        _seed_other_watch_line("KO:2026-07-20", crid="KO-2026-07-20-entry-t1", uic=307)
        broker = _BrokerWithPositions([_live_long(307)])
        placer, submissions = _placer(self, broker, _plan((0, 10.0, 100)))
        with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
            self.assertTrue(placer(_pick()))
        self.assertEqual(len(submissions), 1)
        self.assertIn("watch", submissions[0]["note"])

    def test_first_live_uic_deferral_logs_warning_then_debug(self) -> None:
        _journal(self)
        broker = _BrokerWithPositions([_live_long(307)])
        placer, _submissions = _placer(self, broker, _plan((0, 10.0, 100)))
        with (
            mock.patch.dict("os.environ", {_ENV: "50"}, clear=True),
            self.assertLogs(cl.logger, level="DEBUG") as captured,
        ):
            self.assertFalse(placer(_pick()))  # first deferral -> WARNING
            self.assertFalse(placer(_pick()))  # every later tick -> DEBUG
        records = [r for r in captured.records if "live long" in r.getMessage()]
        self.assertEqual([r.levelname for r in records], ["WARNING", "DEBUG"])
        self.assertIn("KO", records[0].getMessage())
        self.assertIn("stays armed", records[0].getMessage())


# --------------------------------------------------------------------------
# Stale tranche_plan retraction on unfired watch end (2026-08-19 adj. F3)
# --------------------------------------------------------------------------


def _seed_terminal_watch(
    *,
    crid: str,
    pick_key: str = "KO:2026-07-20",
    uic: int = 307,
    terminal_kind: str | None = entry_trails.KIND_EXPIRED,
) -> None:
    entry_trails.append_entry_trail_line(
        {
            "kind": entry_trails.KIND_WATCH_OPEN,
            "crid": crid,
            "limit": 10.0,
            "qty": 100.0,
            "pick_key": pick_key,
            "uic": uic,
            "ticker": "KO",
            "exchange_mic": "XNYS",
        }
    )
    if terminal_kind is not None:
        entry_trails.append_entry_trail_line({"kind": terminal_kind, "crid": crid})


class TestStaleTranchePlanRetraction(unittest.TestCase):
    """Adjudication finding 3 (2026-08-19): the watch routing journals the
    tranche_plan at watch-OPEN, so a watch that ends without ANY fill (expired /
    suspended / cancelled) left its ladder governing the uic FOREVER — an
    order-free stale ladder that a later long on the uic (protection-pass
    covering, manual buy + manual stop) would be silently sold down. The watch
    pass now retracts the plan once the pick's watch is fully terminal and
    unfired."""

    def _seed_plan(self, pick_key: str | None = "KO:2026-07-20", uic: int = 307) -> None:
        _seed_plan_line(pick_key=pick_key, uic=uic)

    def _sweep(self) -> None:
        cl._retract_stale_tranche_plans(entry_trails.read_entry_trail_fold())

    def _retractions(self, stops_path: Path) -> list[dict[str, Any]]:
        return [ln for ln in _lines(stops_path) if ln["kind"] == "tranche_plan_retracted"]

    def test_all_terminal_unfired_pick_retracts_its_plan(self) -> None:
        for terminal in (
            entry_trails.KIND_EXPIRED,
            entry_trails.KIND_SUSPENDED,
            entry_trails.KIND_CANCELLED,
        ):
            with self.subTest(terminal=terminal):
                _journal(self)
                stops_path = _planned_journal(self)
                _seed_terminal_watch(crid="KO-2026-07-20-entry-t0", terminal_kind=terminal)
                self._seed_plan()
                self._sweep()
                retractions = self._retractions(stops_path)
                self.assertEqual(len(retractions), 1)
                self.assertEqual(retractions[0]["uic"], 307)
                self.assertEqual(retractions[0]["pick_key"], "KO:2026-07-20")
                # The fold no longer governs the uic — the live-exit engine
                # will never adopt a later position onto the stale ladder.
                self.assertNotIn(307, cl.fold_tranche_plans(_lines(stops_path)))

    def test_a_fired_tier_blocks_retraction(self) -> None:
        # A fired pick is NEVER the unfired class's candidate. Without deps
        # (this direct call) the #1223 fired class is skipped entirely; with
        # deps it still demands closure evidence + a net-flat book + the
        # two-tick latch — see TestFiredTerminalPlanRetraction.
        _journal(self)
        stops_path = _planned_journal(self)
        _seed_terminal_watch(crid="KO-2026-07-20-entry-t0", terminal_kind=entry_trails.KIND_FIRED)
        _seed_terminal_watch(crid="KO-2026-07-20-entry-t1", terminal_kind=entry_trails.KIND_EXPIRED)
        self._seed_plan()
        self._sweep()
        self.assertEqual(self._retractions(stops_path), [])

    def test_a_still_open_tier_blocks_retraction(self) -> None:
        _journal(self)
        stops_path = _planned_journal(self)
        _seed_terminal_watch(crid="KO-2026-07-20-entry-t0", terminal_kind=entry_trails.KIND_EXPIRED)
        _seed_terminal_watch(crid="KO-2026-07-20-entry-t1", terminal_kind=None)  # still watching
        self._seed_plan()
        self._sweep()
        self.assertEqual(self._retractions(stops_path), [])

    def test_a_plan_governed_by_a_newer_pick_is_not_retracted(self) -> None:
        _journal(self)
        stops_path = _planned_journal(self)
        _seed_terminal_watch(crid="KO-2026-07-20-entry-t0")
        self._seed_plan(pick_key="KO:2026-08-01")  # a NEWER pick owns the uic now
        self._sweep()
        self.assertEqual(self._retractions(stops_path), [])

    def test_a_keyless_governing_plan_is_never_retracted(self) -> None:
        # A bracket-path plan (no pick_key) on the same uic is coupled to a
        # real placement — the sweep must never touch it.
        _journal(self)
        stops_path = _planned_journal(self)
        _seed_terminal_watch(crid="KO-2026-07-20-entry-t0")
        self._seed_plan(pick_key=None)
        self._sweep()
        self.assertEqual(self._retractions(stops_path), [])

    def test_retraction_is_idempotent(self) -> None:
        _journal(self)
        stops_path = _planned_journal(self)
        _seed_terminal_watch(crid="KO-2026-07-20-entry-t0")
        self._seed_plan()
        self._sweep()
        self._sweep()
        self.assertEqual(len(self._retractions(stops_path)), 1)

    def test_watch_pass_runs_the_sweep(self) -> None:
        _journal(self)
        stops_path = _planned_journal(self)
        _seed_terminal_watch(crid="KO-2026-07-20-entry-t0")
        self._seed_plan()
        deps = _watch_deps(_FakeFeed({}), [])
        with mock.patch.dict("os.environ", _ALLOW, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())
        self.assertEqual(len(self._retractions(stops_path)), 1)

    def test_a_journal_failure_never_aborts_the_sweep_caller(self) -> None:
        _journal(self)
        _planned_journal(self)
        _seed_terminal_watch(crid="KO-2026-07-20-entry-t0")
        self._seed_plan()

        def boom(_line: Any) -> None:
            raise OSError("disk full")

        with (
            mock.patch.object(cl, "_append_standalone_stop_journal", boom),
            self.assertLogs(cl.logger, level="WARNING") as captured,
        ):
            self._sweep()  # must swallow + warn, never raise
        self.assertTrue(any("retraction" in msg for msg in captured.output))


def _seed_planned_fire_line(crid: str = "KO-2026-07-20-entry-t0-fire", uic: int = 307) -> None:
    cl._append_standalone_stop_journal(
        cl._build_planned_line(
            entry_crid=crid,
            uic=uic,
            side="SELL",
            stop_price=9.0,
            take_profit=None,
            tier_index=0,
        )
    )


class TestStalePlannedLineRetraction(unittest.TestCase):
    """#1249 part 2, class (a): the fire-arm ``planned`` write-ahead of a pick
    whose watch ended with NO fill covers nothing — the sweep retracts it so a
    stale line can never conflict a later fill on the uic (the merge-refusal
    deadlock). Keyed per-crid, orthogonal to tranche governance; a fired or
    still-open tier blocks the class exactly like the tranche sweep."""

    def _sweep(self) -> None:
        cl._retract_stale_tranche_plans(entry_trails.read_entry_trail_fold())

    def _markers(self, stops_path: Path) -> list[dict[str, Any]]:
        return [ln for ln in _lines(stops_path) if ln["kind"] == "planned_retracted"]

    def test_unfired_terminal_pick_retracts_its_planned_lines(self) -> None:
        for terminal in (
            entry_trails.KIND_EXPIRED,
            entry_trails.KIND_SUSPENDED,
            entry_trails.KIND_CANCELLED,
        ):
            with self.subTest(terminal=terminal):
                _journal(self)
                stops_path = _planned_journal(self)
                _seed_terminal_watch(crid="KO-2026-07-20-entry-t0", terminal_kind=terminal)
                _seed_planned_fire_line()
                self._sweep()
                markers = self._markers(stops_path)
                self.assertEqual(len(markers), 1)
                self.assertEqual(markers[0]["client_request_id"], "KO-2026-07-20-entry-t0-fire")
                self.assertEqual(markers[0]["uic"], 307)
                self.assertNotIn(307, cl._fold_planned_exits(_lines(stops_path)))

    def test_a_fired_tier_blocks_the_unfired_class(self) -> None:
        # The fired pick's planned line may still cover a LIVE position — the
        # unfired class never touches it (the fired class has its own gates).
        _journal(self)
        stops_path = _planned_journal(self)
        _seed_terminal_watch(crid="KO-2026-07-20-entry-t0", terminal_kind=entry_trails.KIND_FIRED)
        _seed_terminal_watch(crid="KO-2026-07-20-entry-t1", terminal_kind=entry_trails.KIND_EXPIRED)
        _seed_planned_fire_line()
        _seed_planned_fire_line(crid="KO-2026-07-20-entry-t1-fire")
        self._sweep()
        self.assertEqual(self._markers(stops_path), [])

    def test_a_still_open_tier_blocks(self) -> None:
        _journal(self)
        stops_path = _planned_journal(self)
        _seed_terminal_watch(crid="KO-2026-07-20-entry-t0", terminal_kind=entry_trails.KIND_EXPIRED)
        _seed_terminal_watch(crid="KO-2026-07-20-entry-t1", terminal_kind=None)  # still watching
        _seed_planned_fire_line()
        self._sweep()
        self.assertEqual(self._markers(stops_path), [])

    def test_retraction_is_idempotent(self) -> None:
        _journal(self)
        stops_path = _planned_journal(self)
        _seed_terminal_watch(crid="KO-2026-07-20-entry-t0")
        _seed_planned_fire_line()
        self._sweep()
        self._sweep()
        self.assertEqual(len(self._markers(stops_path)), 1)

    def test_planned_retraction_ignores_tranche_governance(self) -> None:
        # A NEWER pick may already govern the uic's tranche ladder — the ended
        # pick's own fire-crid planned line is retracted regardless (per-crid
        # key, so the newer pick's lines are structurally untouchable), while
        # the newer pick's tranche plan is left alone.
        _journal(self)
        stops_path = _planned_journal(self)
        _seed_terminal_watch(crid="KO-2026-07-20-entry-t0")
        _seed_plan_line(pick_key="KO:2026-08-01")  # a NEWER pick owns the ladder
        _seed_planned_fire_line()
        self._sweep()
        self.assertEqual(len(self._markers(stops_path)), 1)
        tranche_retractions = [
            ln for ln in _lines(stops_path) if ln["kind"] == "tranche_plan_retracted"
        ]
        self.assertEqual(tranche_retractions, [])

    def test_a_tier_that_never_armed_leaves_no_marker(self) -> None:
        # An expired tier that never reached the fire-arm wrote no planned line
        # — nothing to retract, and the sweep writes nothing.
        _journal(self)
        stops_path = _planned_journal(self)
        _seed_terminal_watch(crid="KO-2026-07-20-entry-t0")
        self._sweep()
        self.assertEqual(self._markers(stops_path), [])

    def test_watch_pass_runs_the_sweep(self) -> None:
        _journal(self)
        stops_path = _planned_journal(self)
        _seed_terminal_watch(crid="KO-2026-07-20-entry-t0")
        _seed_planned_fire_line()
        deps = _watch_deps(_FakeFeed({}), [])
        with mock.patch.dict("os.environ", _ALLOW, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())
        self.assertEqual(len(self._markers(stops_path)), 1)

    def test_a_read_failure_in_the_helper_warns_and_returns_zero(self) -> None:
        _journal(self)
        _planned_journal(self)

        def boom() -> Any:
            raise OSError("disk gone")

        with (
            mock.patch.object(cl, "_iter_standalone_stop_journal", boom),
            self.assertLogs(cl.logger, level="WARNING") as captured,
        ):
            count = cl._retract_planned_lines(["x-fire"], note="test")
        self.assertEqual(count, 0)
        self.assertTrue(any("planned" in msg for msg in captured.output))


def _seed_plan_line(pick_key: str | None = "KO:2026-07-20", uic: int = 307) -> None:
    line: dict[str, Any] = {
        "kind": "tranche_plan",
        "uic": uic,
        "tp_tranches": [
            {
                "tranche_index": 0,
                "target_price": 14.0,
                "tranche_frac": 1.0,
                "r_multiple": 0.0,
                "tag": "geometry",
            }
        ],
        "reference_qty": 100.0,
        "stop_price": 8.0,
    }
    if pick_key is not None:
        line["pick_key"] = pick_key
    cl._append_standalone_stop_journal(line)


def _seed_stop_filled(
    *,
    uic: int = 307,
    ref: str | None = "KO-2026-07-20-entry-t0-stop-1",
    partial: bool = False,
    order_id: str = "S-1223",
) -> None:
    cl._append_standalone_stop_journal(
        {
            "kind": "stop_filled",
            "uic": uic,
            "order_id": order_id,
            "qty": 100.0,
            "avg_price": 9.0,
            "ref": ref,
            "partial": partial,
            "ts": 1_700_000_000.0,
        }
    )


def _seed_position_closing_fire(*, uic: int = 307) -> None:
    cl._append_standalone_stop_journal(
        {"kind": "tranche_fired", "uic": uic, "tag": "tp1", "position_closed": True}
    )


class _CountingPositionsBroker(_BrokerWithPositions):
    """Counts ``get_positions`` reads — pins the fired-class sweep's lazy-fetch
    contract (zero extra REST calls without a fired candidate)."""

    def __init__(self, positions: list[Any]) -> None:
        super().__init__(positions)
        self.position_reads = 0

    def get_positions(self) -> list:
        self.position_reads += 1
        return super().get_positions()


class _ExplodingPositionsBroker(_RecordingBroker):
    def get_positions(self) -> list:
        raise RuntimeError("positions endpoint down")


class TestFiredTerminalPlanRetraction(unittest.TestCase):
    """#1223: a round-tripped pick's ``tranche_plan`` must stop governing the
    uic. A fired tier whose position CLOSED (durable ``stop_filled`` /
    ``tranche_fired[position_closed]`` evidence since the current plan
    generation) and whose uic is NET-FLAT at the broker is a fully terminal,
    flat pick — its plan is the exact stale-ladder hazard the unfired sweep
    already closes. Retraction is belt-and-braces gated: journal closure
    evidence AND a positions read AND a two-consecutive-tick confirmation
    latch (a wrongly retracted plan on a LIVE position would strip its exit
    management — worse than the hazard being fixed)."""

    def _seed_fired_pick(self) -> None:
        _seed_terminal_watch(crid="KO-2026-07-20-entry-t0", terminal_kind=entry_trails.KIND_FIRED)
        _seed_terminal_watch(
            crid="KO-2026-07-20-entry-t1", terminal_kind=entry_trails.KIND_CANCELLED
        )

    def _deps(self, broker: Any) -> cl.LoopDeps:
        return _watch_deps(_FakeFeed({}), [], broker=broker)

    def _sweep(self, deps: cl.LoopDeps) -> None:
        cl._retract_stale_tranche_plans(entry_trails.read_entry_trail_fold(), deps)

    def _retractions(self, stops_path: Path) -> list[dict[str, Any]]:
        return [ln for ln in _lines(stops_path) if ln["kind"] == "tranche_plan_retracted"]

    def test_round_tripped_flat_pick_retracts_on_the_second_sweep(self) -> None:
        _journal(self)
        stops_path = _planned_journal(self)
        self._seed_fired_pick()
        _seed_plan_line()
        _seed_stop_filled()
        deps = self._deps(_BrokerWithPositions([]))
        self._sweep(deps)
        self.assertEqual(self._retractions(stops_path), [])  # first sight: latch only
        self._sweep(deps)
        retractions = self._retractions(stops_path)
        self.assertEqual(len(retractions), 1)
        self.assertEqual(retractions[0]["uic"], 307)
        self.assertEqual(retractions[0]["pick_key"], "KO:2026-07-20")
        self.assertNotIn(307, cl.fold_tranche_plans(_lines(stops_path)))

    def test_a_position_closing_tranche_also_counts_as_closure(self) -> None:
        _journal(self)
        stops_path = _planned_journal(self)
        self._seed_fired_pick()
        _seed_plan_line()
        _seed_position_closing_fire()
        deps = self._deps(_BrokerWithPositions([]))
        self._sweep(deps)
        self._sweep(deps)
        self.assertEqual(len(self._retractions(stops_path)), 1)

    def test_eod_netting_rows_net_to_flat(self) -> None:
        # LIVE Saxo EOD netting: an intraday round trip leaves +q and -q rows
        # until the nightly netting — net-flat must count as flat.
        _journal(self)
        stops_path = _planned_journal(self)
        self._seed_fired_pick()
        _seed_plan_line()
        _seed_stop_filled()
        deps = self._deps(
            _BrokerWithPositions([_live_long(307, qty=8.0), _live_long(307, qty=-8.0)])
        )
        self._sweep(deps)
        self._sweep(deps)
        self.assertEqual(len(self._retractions(stops_path)), 1)

    def test_a_net_open_uic_blocks_and_resets_the_latch(self) -> None:
        _journal(self)
        stops_path = _planned_journal(self)
        self._seed_fired_pick()
        _seed_plan_line()
        _seed_stop_filled()
        positions: list[Any] = []
        deps = self._deps(_BrokerWithPositions(positions))
        self._sweep(deps)  # flat -> latched
        positions.append(_live_long(307, qty=5.0))
        self._sweep(deps)  # OPEN -> blocked AND latch cleared
        self.assertEqual(self._retractions(stops_path), [])
        positions.clear()
        self._sweep(deps)  # flat again -> must re-latch, not retract
        self.assertEqual(self._retractions(stops_path), [])
        self._sweep(deps)
        self.assertEqual(len(self._retractions(stops_path)), 1)

    def test_no_closure_evidence_never_retracts(self) -> None:
        # Fired tier + expired sibling + flat-LOOKING book but no durable
        # closure record: the position may be held (sibling TTL expiry) or was
        # closed manually — either way the ladder must stand and the positions
        # endpoint must not even be consulted.
        _journal(self)
        stops_path = _planned_journal(self)
        _seed_terminal_watch(crid="KO-2026-07-20-entry-t0", terminal_kind=entry_trails.KIND_FIRED)
        _seed_terminal_watch(crid="KO-2026-07-20-entry-t1", terminal_kind=entry_trails.KIND_EXPIRED)
        _seed_plan_line()
        broker = _CountingPositionsBroker([])
        deps = self._deps(broker)
        self._sweep(deps)
        self._sweep(deps)
        self.assertEqual(self._retractions(stops_path), [])
        self.assertEqual(broker.position_reads, 0)

    def test_a_partial_stop_fill_is_not_closure(self) -> None:
        _journal(self)
        stops_path = _planned_journal(self)
        self._seed_fired_pick()
        _seed_plan_line()
        _seed_stop_filled(partial=True)
        deps = self._deps(_BrokerWithPositions([]))
        self._sweep(deps)
        self._sweep(deps)
        self.assertEqual(self._retractions(stops_path), [])

    def test_round_trip_confirmation_also_retracts_the_planned_lines(self) -> None:
        # #1249 part 2, class (b): the confirmed round-trip write retracts the
        # pick's fire-crid planned lines alongside the tranche plan — riding the
        # SAME belt-and-braces gates (closure evidence + net-flat book + the
        # two-tick latch), never before them.
        _journal(self)
        stops_path = _planned_journal(self)
        self._seed_fired_pick()
        _seed_plan_line()
        _seed_stop_filled()
        _seed_planned_fire_line()
        deps = self._deps(_BrokerWithPositions([]))
        self._sweep(deps)
        planned_markers = [ln for ln in _lines(stops_path) if ln["kind"] == "planned_retracted"]
        self.assertEqual(planned_markers, [])  # first sight: latch only
        self._sweep(deps)
        planned_markers = [ln for ln in _lines(stops_path) if ln["kind"] == "planned_retracted"]
        self.assertEqual(len(planned_markers), 1)
        self.assertEqual(planned_markers[0]["client_request_id"], "KO-2026-07-20-entry-t0-fire")
        self.assertNotIn(307, cl._fold_planned_exits(_lines(stops_path)))

    def test_another_picks_stop_fill_is_not_closure(self) -> None:
        # uic reuse defense: a stop ref parsing to a DIFFERENT pick key is not
        # this pick's round trip.
        _journal(self)
        stops_path = _planned_journal(self)
        self._seed_fired_pick()
        _seed_plan_line()
        _seed_stop_filled(ref="ZZ-2026-01-05-entry-t0-stop-1")
        deps = self._deps(_BrokerWithPositions([]))
        self._sweep(deps)
        self._sweep(deps)
        self.assertEqual(self._retractions(stops_path), [])

    def test_closure_before_the_current_plan_generation_does_not_count(self) -> None:
        _journal(self)
        stops_path = _planned_journal(self)
        self._seed_fired_pick()
        _seed_stop_filled()  # older generation's round trip...
        _seed_plan_line()  # ...reset by the current plan line
        deps = self._deps(_BrokerWithPositions([]))
        self._sweep(deps)
        self._sweep(deps)
        self.assertEqual(self._retractions(stops_path), [])

    def test_unresolvable_position_rows_skip_the_class(self) -> None:
        _journal(self)
        stops_path = _planned_journal(self)
        self._seed_fired_pick()
        _seed_plan_line()
        _seed_stop_filled()
        unresolvable = type("Pos", (), {"instrument": None, "quantity": 1.0})()
        deps = self._deps(_BrokerWithPositions([unresolvable]))
        self._sweep(deps)
        self._sweep(deps)
        self.assertEqual(self._retractions(stops_path), [])

    def test_a_positions_read_failure_skips_fired_class_not_unfired(self) -> None:
        _journal(self)
        stops_path = _planned_journal(self)
        self._seed_fired_pick()
        _seed_plan_line()
        _seed_stop_filled()
        # An UNFIRED terminal pick on another uic in the same sweep.
        _seed_terminal_watch(
            crid="OT-2026-07-19-entry-t0",
            pick_key="OT:2026-07-19",
            uic=42,
            terminal_kind=entry_trails.KIND_EXPIRED,
        )
        _seed_plan_line(pick_key="OT:2026-07-19", uic=42)
        deps = self._deps(_ExplodingPositionsBroker())
        self._sweep(deps)
        self._sweep(deps)
        retractions = self._retractions(stops_path)
        self.assertEqual([r["uic"] for r in retractions], [42])

    def test_a_broker_without_get_positions_skips_quietly(self) -> None:
        _journal(self)
        stops_path = _planned_journal(self)
        self._seed_fired_pick()
        _seed_plan_line()
        _seed_stop_filled()
        deps = _watch_deps(_FakeFeed({}), [])  # broker=object(), the default test deps
        with self.assertNoLogs(cl.logger, level="WARNING"):
            self._sweep(deps)
            self._sweep(deps)
        self.assertEqual(self._retractions(stops_path), [])

    def test_no_positions_read_without_a_fired_candidate(self) -> None:
        _journal(self)
        stops_path = _planned_journal(self)
        _seed_terminal_watch(crid="KO-2026-07-20-entry-t0", terminal_kind=entry_trails.KIND_EXPIRED)
        _seed_plan_line()
        broker = _CountingPositionsBroker([])
        deps = self._deps(broker)
        self._sweep(deps)
        self.assertEqual(len(self._retractions(stops_path)), 1)  # unfired class unaffected
        self.assertEqual(broker.position_reads, 0)

    def test_a_newer_picks_plan_is_never_retracted(self) -> None:
        _journal(self)
        stops_path = _planned_journal(self)
        self._seed_fired_pick()
        _seed_plan_line(pick_key="KO:2026-08-01")  # a NEWER pick owns the uic now
        _seed_stop_filled()
        deps = self._deps(_BrokerWithPositions([]))
        self._sweep(deps)
        self._sweep(deps)
        self.assertEqual(self._retractions(stops_path), [])

    def test_an_outer_sweep_failure_also_clears_the_latch(self) -> None:
        # #1223 zen M2: the latch contract is "every retraction rests on two
        # consecutive CLEAN observations" — a tick whose journal read blew up
        # observed nothing, so the latch from before it must not survive.
        _journal(self)
        stops_path = _planned_journal(self)
        self._seed_fired_pick()
        _seed_plan_line()
        _seed_stop_filled()
        deps = self._deps(_BrokerWithPositions([]))
        self._sweep(deps)  # clean -> latched

        def boom() -> Any:
            raise OSError("journal unreadable")

        with (
            mock.patch.object(cl, "_iter_standalone_stop_journal", boom),
            self.assertLogs(cl.logger, level="WARNING"),
        ):
            self._sweep(deps)  # outer failure -> latch must clear
        self._sweep(deps)  # clean again -> re-latch only
        self.assertEqual(self._retractions(stops_path), [])
        self._sweep(deps)
        self.assertEqual(len(self._retractions(stops_path)), 1)

    def test_watch_pass_reaches_the_fired_class_end_to_end(self) -> None:
        _journal(self)
        stops_path = _planned_journal(self)
        self._seed_fired_pick()
        _seed_plan_line()
        _seed_stop_filled()
        deps = self._deps(_BrokerWithPositions([]))
        with mock.patch.dict("os.environ", _ALLOW, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())
        self.assertEqual(len(self._retractions(stops_path)), 1)


if __name__ == "__main__":
    unittest.main()

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

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager import entry_trails
from broker_contract.sizing import SetupPlan, TierPlan

_ENV = entry_trails.ENTRY_TRAIL_BPS_ENV


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _pick(ticker: str = "KO", date: str = "2026-07-20") -> Any:
    return type(
        "Intent",
        (),
        {
            "instrument": type("Hint", (), {"ticker": ticker, "mic": "XNYS"})(),
            "meta": type("Meta", (), {"brief_date": date})(),
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
        return self.open_orders

    def get_order(self, order_id: str) -> Any:
        from broker_contract.contract import BrokerError

        state = self.order_states.get(order_id)
        if state is None:
            raise BrokerError(f"unknown order {order_id}")
        return state


class _FakeFeed:
    """A price feed keyed by uic; a missing/None entry vetoes (returns None)."""

    def __init__(self, prices: dict[int, float | None]) -> None:
        self._prices = prices

    def latest(self, uic: int) -> Any:
        price = self._prices.get(uic)
        if price is None:
            return None
        return type("PP", (), {"bid": price, "ask": price})()


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
        (f"{pkg}.automanager.reconcile_bridge.verdicts", lambda _r, _b: []),
        (f"{pkg}.automanager.safety.check", lambda *_a, **_k: object()),
        (f"{pkg}.routing.resolve_us_instrument", lambda _b, _t: _instr()),
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


def _watch_deps(feed: Any, alerts: list[tuple[str, str]], broker: Any = None) -> cl.LoopDeps:
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
        verdicts_fn=lambda _r, _b: [],
        build_position_view=lambda _b, _r: object(),
        build_protection_view=lambda _b, _r: object(),
        execute_protection=lambda _a, _k, _r: None,
        sweep_orphans_fn=lambda _b: [],
        alert=lambda _m: None,
        alert_throttled=_throttled,
        live_exits_feed_factory=lambda _u2i: feed,
    )


def _seed_watch(
    path: Path,
    *,
    crid: str,
    limit: float,
    next_tier_limit: float | None,
    window_end: str | None = None,
    d_bps: int = 50,
) -> None:
    entry_trails.append_entry_trail_line(
        {
            "kind": entry_trails.KIND_WATCH_OPEN,
            "crid": crid,
            "limit": limit,
            "qty": 100.0,
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
    )


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


if __name__ == "__main__":
    unittest.main()

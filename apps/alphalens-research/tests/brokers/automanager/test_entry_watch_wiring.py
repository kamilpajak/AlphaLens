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


class _RecordingBroker:
    """Records every order-facing call so a test can assert NONE happened on the
    flag-ON dry-run path."""

    def __init__(self) -> None:
        self.brackets: list[Any] = []
        self.stops: list[Any] = []
        self.amends: list[Any] = []
        self.cancels: list[str] = []

    def get_account(self) -> Any:
        return _acct()

    def get_positions(self) -> list:
        return []

    def place_bracket_order(self, bracket: Any) -> Any:
        self.brackets.append(bracket)
        return type("Placed", (), {"entry_order_id": "E-1", "exit_order_ids": ()})()

    def place_standalone_stop(self, *a: Any, **k: Any) -> Any:
        self.stops.append((a, k))
        return type("Placed", (), {"entry_order_id": "S-1", "exit_order_ids": ()})()

    def amend_stop_amount(self, *a: Any, **k: Any) -> Any:
        self.amends.append((a, k))
        return type("Placed", (), {"entry_order_id": "", "exit_order_ids": ()})()

    def cancel_order(self, order_id: str) -> None:
        self.cancels.append(order_id)


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


def _watch_deps(feed: Any, alerts: list[tuple[str, str]]) -> cl.LoopDeps:
    def _throttled(message: str, reason: str) -> bool:
        alerts.append((message, reason))
        return True

    return cl.LoopDeps(
        broker=object(),
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
            "entry_mode": "entry-trail-dryrun-d50-testcfg",
        }
    )


class TestEntryWatchPassStateMachine(unittest.TestCase):
    def _run(self, deps: cl.LoopDeps, price: float | None, feed: dict[int, float | None]) -> None:
        feed[307] = price
        with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())

    def test_watching_to_touched_to_would_fire(self) -> None:
        path = _journal(self)
        _seed_watch(path, crid="KO-2026-07-20-entry-t0", limit=10.0, next_tier_limit=None)
        prices: dict[int, float | None] = {}
        alerts: list[tuple[str, str]] = []
        deps = _watch_deps(_FakeFeed(prices), alerts)

        self._run(deps, 10.0, prices)  # touch @ limit, trough=10.0
        self._run(deps, 9.90, prices)  # new low, trough=9.90
        self._run(deps, 9.95, prices)  # bounce >= 9.90*1.005=9.9495 -> would fire

        kinds = [line["kind"] for line in _lines(path)]
        self.assertIn(entry_trails.KIND_TOUCHED, kinds)
        self.assertIn(entry_trails.KIND_TROUGH, kinds)
        self.assertIn(entry_trails.KIND_TRAIL_ARMED, kinds)
        # The dry-run would-fire alert fired ...
        self.assertTrue(any("would fire" in m for m, _k in alerts))
        # ... and a terminal `fired` line (entry_order_id=null — no order)
        # closes the tier so it releases its reservation and never re-fires.
        fired = [line for line in _lines(path) if line["kind"] == entry_trails.KIND_FIRED]
        self.assertEqual(len(fired), 1)
        self.assertIsNone(fired[0]["entry_order_id"])  # DRY-RUN: no order

    def test_resumed_would_fire_writes_exactly_one_fired_line(self) -> None:
        # Regression guard (crash between the trail_armed line and the
        # synthesized fired line): a watcher RESUMED in WOULD_FIRE writes the
        # terminal fired line EXACTLY ONCE — after it the tier carries a
        # terminal marker and leaves the active set, so no later tick can
        # re-persist it. Critical for T2: a resumed WOULD_FIRE must never
        # re-emit a fire.
        path = _journal(self)
        _seed_watch(path, crid="KO-2026-07-20-entry-t0", limit=10.0, next_tier_limit=None)
        # A trail_armed line with NO following fired line == the crash window.
        entry_trails.append_entry_trail_line(
            {
                "kind": entry_trails.KIND_TRAIL_ARMED,
                "crid": "KO-2026-07-20-entry-t0",
                "trigger": 9.95,
            }
        )
        prices: dict[int, float | None] = {}
        deps = _watch_deps(_FakeFeed(prices), [])
        self._run(deps, 9.95, prices)  # resume tick: one fired line written
        self._run(deps, 9.96, prices)  # tier now terminal -> not re-persisted
        fired = [line for line in _lines(path) if line["kind"] == entry_trails.KIND_FIRED]
        self.assertEqual(len(fired), 1)

    def test_deep_decline_suspends_below_next_tier(self) -> None:
        path = _journal(self)
        _seed_watch(path, crid="KO-2026-07-20-entry-t0", limit=10.0, next_tier_limit=9.5)
        prices: dict[int, float | None] = {}
        alerts: list[tuple[str, str]] = []
        deps = _watch_deps(_FakeFeed(prices), alerts)
        self._run(deps, 10.0, prices)  # touched
        self._run(deps, 9.40, prices)  # below next tier 9.5 -> suspended
        suspended = [line for line in _lines(path) if line["kind"] == entry_trails.KIND_SUSPENDED]
        self.assertEqual(len(suspended), 1)
        self.assertTrue(any("suspended" in m for m, _k in alerts))

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
        deps = _watch_deps(_FakeFeed(prices), [])
        with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())
        expired = [line for line in _lines(path) if line["kind"] == entry_trails.KIND_EXPIRED]
        self.assertEqual(len(expired), 1)

    def test_no_broker_field_is_ever_touched_by_the_pass(self) -> None:
        # The pass takes a bare object() broker in _watch_deps; if it tried any
        # order call it would AttributeError. Drive a full would-fire.
        path = _journal(self)
        _seed_watch(path, crid="KO-2026-07-20-entry-t0", limit=10.0, next_tier_limit=None)
        prices: dict[int, float | None] = {}
        deps = _watch_deps(_FakeFeed(prices), [])
        for price in (10.0, 9.90, 9.95):
            self._run(deps, price, prices)  # no exception == no broker call


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


if __name__ == "__main__":
    unittest.main()

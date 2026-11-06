"""The entry-fill, take-profit and sibling-retire pages go out AFTER protection.

Each of these was sent inline, on the synchronous Telegram POST (34-64 s of
block when the network hangs, not bounded at all on DNS), from a pass that runs
before ``_run_protection_pass`` in the same tick. Measured on the REAL
``run_once`` with a 2 s sink, the protection pass started at +2.0 s behind each
of them, and at +4.0 s behind a full stop fill of a pick with an open sibling
tier (the fill page plus the retire page). The entry-fill page is the worst of
them: it delays the stop of the very position it announces.

They now go through ``_queue_page`` / ``_drain_pages`` like the three stop pages.
The entry-fill, ceiling-breach, take-profit and "retired N sibling tier(s)"
pages follow a journal latch (``fired``, ``tranche_fired``, ``cancelled``) that
keeps a later tick from paging the same event, so a page left in the queue is
lost for good. The retire "skipped" and "journal write error" pages carry no
latch and come back on the next tick; they are queued only to take them off the
protective path. The ``try`` whose ``finally`` drains opens
at the entry-trail reconcile pass, the first pass that queues one, and these
tests pin that a raise anywhere after it still lets the page out. A sink or a
render that raises on one of these pages is contained by the drain: the page is
lost with a WARNING and the tick returns, where on main it left the tick before
the protection pass.
"""

from __future__ import annotations

import dataclasses
import json
import os
from typing import Any
from unittest import mock

import alphalens_pipeline.brokers.automanager.control_loop as cl
from alphalens_pipeline.brokers.automanager import entry_trails
from alphalens_pipeline.brokers.automanager import stop_journal as sj
from broker_contract.contract import OrderStatus

from tests.brokers.automanager import test_entry_watch_reconcile as entry_case
from tests.brokers.automanager import test_live_exits_pass as exits_case
from tests.brokers.automanager import test_stop_fill_reconcile as fill_case
from tests.brokers.automanager.acceptance.fake_broker import FakeBroker
from tests.brokers.automanager.home_isolation import IsolatedHomeTestCase
from tests.brokers.automanager.test_stop_fill_page_deferral import _flat_book

_PROTECTION = "protection"

# The stop-fill pick of ``test_stop_fill_reconcile``: GME, two tiers, E1 filled.
_FILL_KEY = f"stop-fill:{fill_case._STOP_ID}"
_RETIRE_KEY = f"sibling-retire:{fill_case._PICK_KEY}"
_RETIRE_PAGE = (
    "entry-watch: retired 1 sibling tier(s) of GME:2026-08-27 after round-trip (stop S-7727 filled)"
)
_SKIP_KEY = f"sibling-retire-armed:{fill_case._PICK_KEY}"
_SKIP_PAGE = (
    "entry-watch: sibling retire of GME:2026-08-27 skipped — tier GME-2026-08-27-entry-t1 "
    "has a native entry arm (order_id='B-9') — cancel it at the broker first "
    "(`alphalens broker cancel`), then disarm again"
)
_IO_KEY = f"sibling-retire-io:{fill_case._PICK_KEY}"
_IO_PAGE = "entry-watch: sibling retire of GME:2026-08-27 failed — journal write error: disk full"

# The armed KO tier of ``test_entry_watch_reconcile``, filled.
_ENTRY_KEY = f"entry-trail:fired:{entry_case._CRID}"
_ENTRY_PAGE = "BUY KO E1 100 @ 10.05 (order TR-1)"
_BREACH_KEY = f"entry-trail:ceiling-breach:{entry_case._CRID}"
_BREACH_PAGE = (
    "entry-trail KO E1: filled 10.12 ABOVE its 10.07 ceiling (+49.7 bps) — "
    "the G1 clamp is not enforced on this order type (#1317)"
)

_EXITS_ENV = {exits_case._LIVE_EXITS_ENV: "1", exits_case._ALLOW_ORDERS_ENV: "1"}

# Every pass ``run_once`` runs after the first one that queues a page.
_SEAMS_AFTER_ENTRY_RECONCILE = (
    "_run_stop_fill_reconcile_pass",
    "_sweep_owed_sibling_retires",
    "_run_placement_drain",
    "_run_entry_watch_pass",
    "_run_verdict_advance",
    "_run_live_exits_pass",
    "_run_protection_pass",
)


class _PageOrderCase(IsolatedHomeTestCase):
    """``run_once`` with a sink that records each page and a spy that records
    the moment the protection pass starts, on one timeline."""

    def setUp(self) -> None:
        super().setUp()
        self.events: list[str] = []
        self.pages: list[tuple[str, str]] = []

        self.failing_key: str | None = None

    def _sink(self, message: str, reason: str) -> bool:
        if reason == self.failing_key:
            raise RuntimeError("sink boom")
        self.events.append(reason)
        self.pages.append((message, reason))
        return True

    def _spied(self, deps: cl.LoopDeps) -> cl.LoopDeps:
        return dataclasses.replace(deps, alert_throttled=self._sink)

    def _tick(self, deps: cl.LoopDeps, env: dict[str, str] | None = None) -> cl.TickReport:
        real = cl._run_protection_pass

        def _spy(*args: Any, **kwargs: Any) -> None:
            self.events.append(_PROTECTION)
            real(*args, **kwargs)

        with (
            mock.patch.object(cl, "_run_protection_pass", _spy),
            mock.patch.dict(os.environ, env or {}),
        ):
            return cl.run_once(self._spied(deps))


class _SiblingCase(_PageOrderCase):
    """A full stop fill of the GME pick, whose second tier is still watching."""

    def _seed(self, *entries: dict[str, Any]) -> cl.LoopDeps:
        stops = fill_case._stop_journal(self)
        fill_case._seed(stops, fill_case._stop_placed(), fill_case._tranche_plan())
        fill_case._seed(fill_case._entry_journal(self), *entries)
        return dataclasses.replace(
            fill_case._deps(fill_case._filled_broker(), []), build_protection_view=_flat_book
        )

    def _open_sibling(self) -> cl.LoopDeps:
        return self._seed(
            {"kind": "fired", "crid": fill_case._E1_CRID, "order_id": "B-1", "realized_qty": 16.0},
            fill_case._watch_open(fill_case._E2_CRID),
        )


class TestTheSiblingRetirePageWaitsForProtection(_SiblingCase):
    def test_the_retire_page_is_sent_after_the_protection_pass(self) -> None:
        self._tick(self._open_sibling())
        self.assertEqual(self.events, [_PROTECTION, _FILL_KEY, _RETIRE_KEY])

    def test_the_retire_page_text_is_unchanged(self) -> None:
        self._tick(self._open_sibling())
        self.assertEqual(self.pages[-1], (_RETIRE_PAGE, _RETIRE_KEY))

    def test_each_page_counts_once_when_sent(self) -> None:
        self.assertEqual(self._tick(self._open_sibling()).alerts, 2)

    def test_the_skip_page_is_sent_after_the_protection_pass(self) -> None:
        deps = self._seed(
            fill_case._watch_open(fill_case._E2_CRID),
            {"kind": "trail_armed", "crid": fill_case._E2_CRID, "order_id": "B-9", "trigger": 17.4},
        )
        self._tick(deps)
        # Once from the stop-fill pass and once from the owed-retire sweep in
        # the same tick; the throttle dedups them in production.
        self.assertEqual(self.pages[1:], [(_SKIP_PAGE, _SKIP_KEY), (_SKIP_PAGE, _SKIP_KEY)])
        self.assertEqual(self.events, [_PROTECTION, _FILL_KEY, _SKIP_KEY, _SKIP_KEY])

    def test_the_journal_failure_page_is_sent_after_the_protection_pass(self) -> None:
        deps = self._open_sibling()
        with mock.patch.object(
            entry_trails, "cancel_open_watches", side_effect=OSError("disk full")
        ):
            self._tick(deps)
        # The restart-safe sweep retries in the same tick and hits the same
        # error: one page per call, the throttle is what dedups them in
        # production. Both after protection.
        self.assertEqual(self.events, [_PROTECTION, _FILL_KEY, _IO_KEY, _IO_KEY])
        self.assertEqual(self.pages[1], (_IO_PAGE, _IO_KEY))

    def test_a_raise_after_the_retire_still_sends_its_page(self) -> None:
        # The ``cancelled`` latch is down before the page: no later tick would
        # retire, or page, that sibling again.
        deps = self._open_sibling()
        with (
            mock.patch.object(cl, "_run_placement_drain", side_effect=RuntimeError("boom")),
            self.assertRaisesRegex(RuntimeError, "^boom$"),
        ):
            self._tick(deps)
        self.assertEqual(self.events, [_FILL_KEY, _RETIRE_KEY])

    def test_a_sink_that_raises_on_the_retire_page_costs_only_that_page(self) -> None:
        # On main this raise left the tick before the protection pass. Queued,
        # it is contained by the drain: the page is lost, the tick returns.
        self.failing_key = _RETIRE_KEY
        with self.assertLogs(cl.logger, level="WARNING") as logs:
            self._tick(self._open_sibling())
        self.assertEqual(self.events, [_PROTECTION, _FILL_KEY])
        self.assertIn("the sibling-retire page failed", logs.output[0])


class _EntryFillCase(_PageOrderCase):
    def _armed_fill(self, fill: float = 10.05) -> cl.LoopDeps:
        path = entry_case._journal(self)
        entry_case._seed_armed(path, order_id="TR-1", limit=10.0)
        broker = entry_case._ResolvingBroker()
        broker.resolutions["TR-1"] = entry_case._os(
            "TR-1", OrderStatus.FILLED, filled_quantity=100.0, avg_fill_price=fill
        )
        return dataclasses.replace(
            entry_case._watch_deps(None, [], broker=broker), build_protection_view=_flat_book
        )

    def _entry_env(self) -> dict[str, str]:
        return {entry_case._ENV: "50"}


class TestTheEntryFillPageWaitsForProtection(_EntryFillCase):
    def test_the_entry_fill_page_is_sent_after_the_protection_pass(self) -> None:
        self._tick(self._armed_fill(), self._entry_env())
        self.assertEqual(self.events, [_PROTECTION, _ENTRY_KEY])

    def test_the_entry_fill_page_text_is_unchanged(self) -> None:
        self._tick(self._armed_fill(), self._entry_env())
        self.assertEqual(self.pages, [(_ENTRY_PAGE, _ENTRY_KEY)])

    def test_the_entry_fill_page_counts_once_when_sent(self) -> None:
        self.assertEqual(self._tick(self._armed_fill(), self._entry_env()).alerts, 1)

    def test_the_ceiling_breach_page_follows_it_after_the_protection_pass(self) -> None:
        self._tick(self._armed_fill(fill=10.12), self._entry_env())
        self.assertEqual(self.events, [_PROTECTION, _ENTRY_KEY, _BREACH_KEY])
        self.assertEqual(self.pages[1], (_BREACH_PAGE, _BREACH_KEY))

    def test_a_raise_in_any_later_pass_still_sends_the_page(self) -> None:
        # The ``fired`` terminal is journaled before the page and takes the tier
        # out of the reconcile fold: no later tick would page this fill again.
        for seam in _SEAMS_AFTER_ENTRY_RECONCILE:
            with self.subTest(seam):
                self.events.clear()
                self.pages.clear()
                deps = self._armed_fill()
                with (
                    mock.patch.object(cl, seam, side_effect=RuntimeError("boom")),
                    self.assertRaisesRegex(RuntimeError, "^boom$"),
                ):
                    self._tick(deps, self._entry_env())
                self.assertEqual(self.pages, [(_ENTRY_PAGE, _ENTRY_KEY)])

    def test_a_raise_later_in_the_same_pass_still_sends_the_page(self) -> None:
        # Why the ``try`` opens at THIS pass and not at the next one: the pass
        # walks the tiers in order, latches and queues the first fill, then the
        # second tier's audit read raises (not a ``BrokerError``) out of it.
        path = entry_case._journal(self)
        entry_case._seed_armed(path, order_id="TR-1", limit=10.0)
        entry_case._seed_armed(path, crid="KO-2026-07-20-entry-t1", order_id="TR-2", limit=9.0)
        broker = _SecondResolveRaises()
        broker.resolutions["TR-1"] = entry_case._os(
            "TR-1", OrderStatus.FILLED, filled_quantity=100.0, avg_fill_price=10.05
        )
        deps = dataclasses.replace(
            entry_case._watch_deps(None, [], broker=broker), build_protection_view=_flat_book
        )
        with self.assertRaisesRegex(RuntimeError, "^boom$"):
            self._tick(deps, self._entry_env())
        self.assertEqual(self.pages, [(_ENTRY_PAGE, _ENTRY_KEY)])

    def test_a_render_that_raises_costs_only_the_page(self) -> None:
        # The render runs in the deferred send: a render defect costs that one
        # page and a WARNING, and no longer leaves the tick before protection.
        with (
            mock.patch.object(cl.trade_alerts, "render", side_effect=ValueError("bad page")),
            self.assertLogs(cl.logger, level="WARNING") as logs,
        ):
            self._tick(self._armed_fill(), self._entry_env())
        self.assertEqual(self.events, [_PROTECTION])
        self.assertIn("the entry-fill page failed", logs.output[0])

    def test_a_sink_that_raises_on_the_breach_page_costs_only_that_page(self) -> None:
        self.failing_key = _BREACH_KEY
        with self.assertLogs(cl.logger, level="WARNING") as logs:
            self._tick(self._armed_fill(fill=10.12), self._entry_env())
        self.assertEqual(self.events, [_PROTECTION, _ENTRY_KEY])
        self.assertIn("the ceiling-breach page failed", logs.output[0])

    def test_the_page_is_queued_by_the_pass_not_sent(self) -> None:
        report = cl.TickReport()
        with mock.patch.dict(os.environ, self._entry_env()):
            cl._run_entry_trail_reconcile_pass(self._spied(self._armed_fill()), report)
        self.assertEqual(self.pages, [])
        cl._drain_pages(report)
        self.assertEqual(self.pages, [(_ENTRY_PAGE, _ENTRY_KEY)])


class _SecondResolveRaises(entry_case._ResolvingBroker):
    """Resolves ``TR-1`` as seeded, then raises on ``TR-2``'s audit read."""

    def resolve_order_outcome(self, order_id: str) -> Any:
        if order_id == "TR-2":
            raise RuntimeError("boom")
        return super().resolve_order_outcome(order_id)


class _TakeProfitCase(_PageOrderCase):
    def setUp(self) -> None:
        super().setUp()
        journal = self.home / "standalone_stops.jsonl"
        patcher = mock.patch.object(sj, "_standalone_stop_journal_path", lambda: journal)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.broker = FakeBroker()

    def _position(self, ticker: str, *, frac: float, pick_key: str | None = None) -> int:
        uic = self.broker.uic_of(ticker)
        self.broker.set_position(ticker, 100, avg_price=15.0)
        self.broker.add_resting_sell(ticker, 100, 13.0, order_type="StopIfTraded")
        sj._append_standalone_stop_journal(
            sj._build_tranche_plan_line(
                uic=uic,
                tp_tranches=(exits_case._tr(0, 16.0, frac),),
                reference_qty=100.0,
                stop_price=13.0,
                pick_key=pick_key,
            )
        )
        return uic

    def _deps(self, *uics: int) -> cl.LoopDeps:
        prices = dict.fromkeys(uics, 16.5)
        return exits_case._deps(
            self.broker,
            alerts=[],
            live_exits_feed_factory=lambda m, *, scope: exits_case._FakeFeed(prices),
        )

    def _tp_pages(self) -> list[tuple[str, str]]:
        return [page for page in self.pages if page[1].startswith("tranche-fired:")]


class TestTheTakeProfitPageWaitsForProtection(_TakeProfitCase):
    def test_the_tp_page_is_sent_after_the_protection_pass(self) -> None:
        uic = self._position("KO", frac=0.5)
        self._tick(self._deps(uic), _EXITS_ENV)
        self.assertEqual(self.events, [_PROTECTION, f"tranche-fired:{uic}:tp1"])

    def test_the_tp_page_text_is_unchanged(self) -> None:
        uic = self._position("KO", frac=0.5)
        self._tick(self._deps(uic), _EXITS_ENV)
        # ``mkt-2``: the fake broker numbers its orders, and the resting stop is 1.
        page = "SELL KO TP1 50 - take-profit 16.00, market sell sent - position still open (order mkt-2)"
        self.assertEqual(self._tp_pages(), [(page, f"tranche-fired:{uic}:tp1")])

    def test_two_tranches_in_one_tick_send_two_distinct_pages(self) -> None:
        # Each queued send must carry ITS tranche: a closure over the loop
        # variable would send the last tranche twice.
        ko = self._position("KO", frac=0.5)
        pep = self._position("PEP", frac=0.5)
        self._tick(self._deps(ko, pep), _EXITS_ENV)
        self.assertEqual(
            sorted(reason for _message, reason in self._tp_pages()),
            sorted([f"tranche-fired:{ko}:tp1", f"tranche-fired:{pep}:tp1"]),
        )
        self.assertEqual(
            sorted(message.split(" ")[1] for message, _reason in self._tp_pages()), ["KO", "PEP"]
        )

    def test_a_raise_in_the_protection_pass_still_sends_the_tp_page(self) -> None:
        uic = self._position("KO", frac=0.5)
        with (
            mock.patch.object(cl, "_run_protection_pass", side_effect=RuntimeError("boom")),
            mock.patch.dict(os.environ, _EXITS_ENV),
            self.assertRaisesRegex(RuntimeError, "^boom$"),
        ):
            cl.run_once(self._spied(self._deps(uic)))
        self.assertEqual([reason for _m, reason in self._tp_pages()], [f"tranche-fired:{uic}:tp1"])

    def test_a_render_that_raises_costs_only_the_tp_page(self) -> None:
        uic = self._position("KO", frac=0.5)
        with (
            mock.patch.object(cl.trade_alerts, "render", side_effect=ValueError("bad page")),
            self.assertLogs(cl.logger, level="WARNING") as logs,
        ):
            self._tick(self._deps(uic), _EXITS_ENV)
        self.assertEqual(self.events, [_PROTECTION])
        self.assertIn("the take-profit page failed", logs.output[0])

    def test_a_closing_tranche_pages_then_retires_after_the_protection_pass(self) -> None:
        uic = self._position("KO", frac=1.0, pick_key="KO:2026-08-27")
        entry_trails._entry_trail_journal_path().parent.mkdir(parents=True, exist_ok=True)
        entry_trails._entry_trail_journal_path().write_text(
            json.dumps(
                {
                    "kind": "watch_open",
                    "crid": "KO-2026-08-27-entry-t1",
                    "pick_key": "KO:2026-08-27",
                    "limit": 14.0,
                    "qty": 10.0,
                }
            )
            + "\n"
        )
        self._tick(self._deps(uic), _EXITS_ENV)
        self.assertEqual(
            self.events,
            [_PROTECTION, f"tranche-fired:{uic}:tp1", "sibling-retire:KO:2026-08-27"],
        )

"""The stop-FILL page is sent after the protection pass, and is never lost.

``_reconcile_one_standing_stop`` used to send the page inline. The sink is a
synchronous Telegram POST (34-64 s of block when the network hangs, and not
bounded at all on DNS), and the stop-fill pass runs near the TOP of
``run_once``, before every pass that protects a position. So one hung page
delayed every other position's stop by the full block. #1621 part 3 deferred the
stop-placed and stop-raised pages through ``_queue_page`` / ``_drain_pages`` for
this reason; this suite pins the stop-fill page onto the same queue.

Three claims, each on the REAL ``run_once`` or the real pass:

* the protection pass STARTS before the stop-fill page's sink is called;
* a raise in any pass between the stop-fill pass and the protection pass still
  lets the page out, and the raise still propagates. The ``stop_filled`` latch
  is journaled BEFORE the page, so the next tick would never page it again: a
  page left in the queue there is lost for good;
* the ticker and the R on the page are the answers read when the fill was
  reconciled, not re-read at the drain, so a journal write between the two
  cannot change what the page says.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path
from typing import Any
from unittest import mock

import alphalens_pipeline.brokers.automanager.control_loop as cl
from alphalens_pipeline.brokers.automanager import entry_trails, state_paths
from alphalens_pipeline.brokers.automanager import stop_journal as sj
from alphalens_pipeline.brokers.automanager.position_manager import ProtectionView
from alphalens_pipeline.brokers.submission_log import iter_submission_records

from tests.brokers.automanager.home_isolation import IsolatedHomeTestCase
from tests.brokers.automanager.test_stop_fill_reconcile import (
    _deps,
    _filled,
    _StopBroker,
)
from tests.brokers.automanager.trades_fixture import SIM_RHI_DIR

_PROTECTION = "protection"
_PAGE_EVENT = "stop-fill page"

# The real RHI:2026-09-03 bracket stop (``test_bracket_stop_ticker``), filled.
_RHI_UIC = 641
_RHI_ORDER = "5040004072"
_RHI_STOP_PLACED = {
    "kind": "stop_placed",
    "order_id": _RHI_ORDER,
    "qty": 701.0,
    "ref": "d1b6f68d-e98c-4a65-a541-74eac07b655d-stop-0",
    "ts": 1788442349.0392632,
    "uic": _RHI_UIC,
}
_RHI_FILL = _filled(_RHI_ORDER, 1796.0, 40.46)
_RHI_FILL_PAGE = "SELL RHI SL 1796 @ 40.46 - plan stop - position closed (order 5040004072)"
_RHI_FILL_KEY = f"stop-fill:{_RHI_ORDER}"
_RHI_FILL_PAGE_BY_UIC = (
    "SELL uic 641 SL 1796 @ 40.46 - plan stop - position closed (order 5040004072)"
)

# The VST:2026-09-21 round trip (``test_exit_alert_result_r``): one tier of 6 @
# 135.11 over a 125.85 plan stop, closed by its stop at 138.66.
_VST_UIC = 7300542
_VST_ORDER = "5448023092"
_VST_STOP_PLACED = {
    "kind": "stop_placed",
    "uic": _VST_UIC,
    "order_id": _VST_ORDER,
    "ref": "VST-2026-09-21-entry-t0-fire-stop-0",
    "qty": 6.0,
    "stop_price": 125.85,
    "ts": 100.0,
}
_VST_FILL_PAGE = (
    "SELL VST SL 6 @ 138.66 - plan stop 125.85 - +0.3834R, position closed (order 5448023092)"
)


def _vst_tier(crid: str, *, qty: float, price: float) -> list[dict[str, Any]]:
    return [
        {
            "kind": "watch_open",
            "crid": crid,
            "pick_key": "VST:2026-09-21",
            "ticker": "VST",
            "disaster_stop": 125.85,
            "uic": _VST_UIC,
            "qty": qty,
            "tier_index": int(crid.rpartition("-t")[2]),
        },
        {"kind": "fired", "crid": crid, "realized_qty": qty, "avg_price": price},
    ]


def _flat_book(_broker: Any, _records: Any) -> ProtectionView:
    """No open position: the real protection pass runs and has nothing to do."""
    return ProtectionView(
        long_positions={},
        all_positions={},
        sell_legs_by_uic={},
        planned_by_uic={},
        oco_unsupported=frozenset(),
    )


_FLAT_BOOK = {"build_protection_view": _flat_book}


class _DeferralCase(IsolatedHomeTestCase):
    """Seeds the journals under the isolated home, where the daemon reads them."""

    def _write_lines(self, path: Path, *lines: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")

    def _seed_rhi_fill(self) -> _StopBroker:
        submissions = state_paths.submissions_path()
        submissions.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SIM_RHI_DIR / "submissions.jsonl", submissions)
        self._write_lines(sj._standalone_stop_journal_path(), _RHI_STOP_PLACED)
        return _StopBroker(outcome=_RHI_FILL)

    def _spied_deps(self, broker: Any, events: list[str], pages: list[tuple[str, str]]) -> Any:
        """``deps`` whose throttled sink logs every page with its key, and
        stamps the stop-fill ones into ``events`` at the moment it is called."""

        def _sink(message: str, reason: str) -> bool:
            if reason.startswith("stop-fill:"):
                events.append(_PAGE_EVENT)
            pages.append((message, reason))
            return True

        return dataclasses.replace(_deps(broker, []), alert_throttled=_sink, **_FLAT_BOOK)

    def _spy_protection(self, events: list[str]) -> Any:
        real = cl._run_protection_pass

        def _spy(*args: Any, **kwargs: Any) -> None:
            events.append(_PROTECTION)
            real(*args, **kwargs)

        return mock.patch.object(cl, "_run_protection_pass", _spy)


class TestTheProtectionPassGoesFirst(_DeferralCase):
    def test_the_protection_pass_starts_before_the_stop_fill_page_is_sent(self) -> None:
        events: list[str] = []
        pages: list[tuple[str, str]] = []
        deps = self._spied_deps(self._seed_rhi_fill(), events, pages)

        with self._spy_protection(events):
            report = cl.run_once(deps)

        self.assertEqual(events, [_PROTECTION, _PAGE_EVENT])
        self.assertEqual(pages, [(_RHI_FILL_PAGE, _RHI_FILL_KEY)])
        self.assertEqual(report.alerts, 1, "the page counts once, when it is sent")

    def test_a_suppressed_page_is_not_counted(self) -> None:
        # The throttle answers False on a repeat: the drain counts only pages
        # that actually went out.
        deps = dataclasses.replace(
            _deps(self._seed_rhi_fill(), []),
            alert_throttled=lambda message, reason: False,
            **_FLAT_BOOK,
        )
        self.assertEqual(cl.run_once(deps).alerts, 0)


class TestARaiseBeforeProtectionStillSendsThePage(_DeferralCase):
    """Every pass ``run_once`` runs between the stop-fill pass and the
    protection pass, made to raise something that is not a ``BrokerError``."""

    _SEAMS = (
        "_sweep_owed_sibling_retires",
        "_run_placement_drain",
        "_run_entry_watch_pass",
        "_run_verdict_advance",
        "_run_live_exits_pass",
    )

    def _assert_sent_and_raised(self, deps: Any, pages: list[tuple[str, str]]) -> None:
        with self.assertRaisesRegex(RuntimeError, "^boom$"):
            cl.run_once(deps)
        self.assertEqual(pages, [(_RHI_FILL_PAGE, _RHI_FILL_KEY)])
        latched = [
            line for line in sj._iter_standalone_stop_journal() if line.get("kind") == "stop_filled"
        ]
        self.assertEqual(len(latched), 1, "the latch is down: no later tick would page it")

    def test_a_raising_pass_in_between_does_not_lose_the_page(self) -> None:
        for seam in self._SEAMS:
            with self.subTest(seam), mock.patch.object(cl, seam, side_effect=RuntimeError("boom")):
                # A fresh stop journal per sub-case: an earlier sub-case's latch
                # would otherwise make this one reconcile nothing.
                sj._standalone_stop_journal_path().unlink(missing_ok=True)
                pages: list[tuple[str, str]] = []
                self._assert_sent_and_raised(
                    self._spied_deps(self._seed_rhi_fill(), [], pages), pages
                )

    def test_an_undecodable_submissions_journal_does_not_lose_the_page(self) -> None:
        # The production reader skips a malformed JSON line but raises on a
        # byte that is not UTF-8. Read through that same reader here, the raise
        # comes from ``deps.read_records``, after the stop-fill pass. The page's
        # own ticker read hits the same byte and falls back to the uic.
        pages: list[tuple[str, str]] = []
        deps = self._spied_deps(self._seed_rhi_fill(), [], pages)
        with state_paths.submissions_path().open("ab") as fh:
            fh.write(b"\xff\n")

        def _production_read() -> list[Any]:
            return list(iter_submission_records(state_paths.submissions_path()))

        with (
            self.assertLogs(cl.logger, level="WARNING"),
            self.assertRaises(UnicodeDecodeError),
        ):
            cl.run_once(dataclasses.replace(deps, read_records=_production_read))

        self.assertEqual(pages, [(_RHI_FILL_PAGE_BY_UIC, _RHI_FILL_KEY)])


class TestARenderFailureCostsOnlyThePage(_DeferralCase):
    def test_a_render_that_raises_is_contained_by_the_drain(self) -> None:
        # The render moved into the deferred send with the delivery: a render
        # defect now costs that one page and a WARNING, as on the stop-placed
        # page, instead of escaping the tick before the protection pass.
        events: list[str] = []
        pages: list[tuple[str, str]] = []
        deps = self._spied_deps(self._seed_rhi_fill(), events, pages)

        with (
            self._spy_protection(events),
            mock.patch.object(cl.trade_alerts, "render", side_effect=ValueError("bad page")),
            self.assertLogs(cl.logger, level="WARNING") as logs,
        ):
            cl.run_once(deps)

        self.assertEqual(events, [_PROTECTION])
        self.assertEqual(pages, [])
        self.assertIn("the stop-fill page failed", logs.output[0])


class _SecondStopRaises(_StopBroker):
    """Resolves the RHI stop as filled, then raises on the next stop's audit
    read: the stop-fill pass itself dies after it queued the first page."""

    def resolve_order_outcome(self, order_id: str) -> Any:
        if order_id != _RHI_ORDER:
            raise RuntimeError("boom")
        return super().resolve_order_outcome(order_id)


class TestARaiseInsideTheStopFillPassStillSendsThePage(_DeferralCase):
    def test_a_later_stop_raising_does_not_lose_an_earlier_page(self) -> None:
        # The pass walks the uics in order (641 before 9999). The first fill is
        # latched and queued; the second stop's read raises out of the pass.
        self._seed_rhi_fill()
        self._write_lines(
            sj._standalone_stop_journal_path(),
            {**_RHI_STOP_PLACED, "uic": 9999, "order_id": "S-9999", "ref": None},
        )
        pages: list[tuple[str, str]] = []
        deps = self._spied_deps(_SecondStopRaises(outcome=_RHI_FILL), [], pages)

        with self.assertRaisesRegex(RuntimeError, "^boom$"):
            cl.run_once(deps)

        self.assertEqual(pages, [(_RHI_FILL_PAGE, _RHI_FILL_KEY)])


class TestThePageSaysWhatWasReadAtTheFill(_DeferralCase):
    """The page is QUEUED by the pass and SENT by the drain. Its facts are read
    at the first moment, so a write in between cannot change them."""

    def _queue(self, broker: Any, pages: list[tuple[str, str]]) -> cl.TickReport:
        report = cl.TickReport()
        cl._run_stop_fill_reconcile_pass(self._spied_deps(broker, [], pages), report)
        self.assertEqual(pages, [], "queued, not sent, by the pass")
        return report

    def test_the_ticker_is_the_one_read_at_the_fill(self) -> None:
        pages: list[tuple[str, str]] = []
        report = self._queue(self._seed_rhi_fill(), pages)
        state_paths.submissions_path().unlink()  # a re-read now names ``uic 641``

        cl._drain_pages(report)

        self.assertEqual(pages, [(_RHI_FILL_PAGE, _RHI_FILL_KEY)])

    def test_the_r_is_the_one_read_at_the_fill(self) -> None:
        self._write_lines(sj._standalone_stop_journal_path(), _VST_STOP_PLACED)
        self._write_lines(
            entry_trails._entry_trail_journal_path(),
            *_vst_tier("VST-2026-09-21-entry-t0", qty=6.0, price=135.11),
        )
        pages: list[tuple[str, str]] = []
        report = self._queue(_StopBroker(outcome=_filled(_VST_ORDER, 6.0, 138.66)), pages)
        # A write between the queue and the drain, by hand: no pass writes
        # this there today. A re-read now finds 10 shares in and 6 out, and
        # refuses the R.
        self._write_lines(
            entry_trails._entry_trail_journal_path(),
            *_vst_tier("VST-2026-09-21-entry-t1", qty=4.0, price=141.11),
        )

        cl._drain_pages(report)

        self.assertEqual(pages, [(_VST_FILL_PAGE, f"stop-fill:{_VST_ORDER}")])
